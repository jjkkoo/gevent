# Copyright (c) 2009-2012 Denis Bilenko. See LICENSE for details.
# cython: auto_pickle=False,embedsignature=True,always_allow_keywords=False

from __future__ import absolute_import, print_function, division

from sys import _getframe as sys_getframe
from sys import exc_info as sys_exc_info
from weakref import ref as wref

# XXX: How to get cython to let us rename this as RawGreenlet
# like we prefer?
from greenlet import greenlet
from greenlet import GreenletExit

from gevent._compat import reraise
from gevent._compat import PYPY as _PYPY
from gevent._tblib import dump_traceback
from gevent._tblib import load_traceback

from gevent.exceptions import InvalidSwitchError

from gevent.hub import iwait
from gevent.hub import wait

from gevent.timeout import Timeout

from gevent._config import config as GEVENT_CONFIG
from gevent._util import Lazy
from gevent._util import readproperty
from gevent._hub_local import get_hub_noargs as get_hub
from gevent import _waiter


__all__ = [
    'Greenlet',
    'joinall',
    'killall',
]


# In Cython, we define these as 'cdef inline' functions. The
# compilation unit cannot have a direct assignment to them (import
# is assignment) without generating a 'lvalue is not valid target'
# error.
locals()['getcurrent'] = __import__('greenlet').getcurrent
locals()['greenlet_init'] = lambda: None
locals()['Waiter'] = _waiter.Waiter


if _PYPY:
    import _continuation # pylint:disable=import-error
    _continulet = _continuation.continulet


class SpawnedLink(object):
    """
    A wrapper around link that calls it in another greenlet.

    Can be called only from main loop.
    """
    __slots__ = ['callback']

    def __init__(self, callback):
        if not callable(callback):
            raise TypeError("Expected callable: %r" % (callback, ))
        self.callback = callback

    def __call__(self, source):
        g = greenlet(self.callback, get_hub())
        g.switch(source)

    def __hash__(self):
        return hash(self.callback)

    def __eq__(self, other):
        return self.callback == getattr(other, 'callback', other)

    def __str__(self):
        return str(self.callback)

    def __repr__(self):
        return repr(self.callback)

    def __getattr__(self, item):
        assert item != 'callback'
        return getattr(self.callback, item)


class SuccessSpawnedLink(SpawnedLink):
    """A wrapper around link that calls it in another greenlet only if source succeed.

    Can be called only from main loop.
    """
    __slots__ = []

    def __call__(self, source):
        if source.successful():
            return SpawnedLink.__call__(self, source)


class FailureSpawnedLink(SpawnedLink):
    """A wrapper around link that calls it in another greenlet only if source failed.

    Can be called only from main loop.
    """
    __slots__ = []

    def __call__(self, source):
        if not source.successful():
            return SpawnedLink.__call__(self, source)

class _Frame(object):

    __slots__ = ('f_code', 'f_lineno', 'f_back')

    def __init__(self, f_code, f_lineno, f_back):
        self.f_code = f_code
        self.f_lineno = f_lineno
        self.f_back = f_back

    @property
    def f_globals(self):
        return None

def _Frame_from_list(frames):
    previous = None
    for frame in reversed(frames):
        f = _Frame(frame[0], frame[1], previous)
        previous = f
    return previous

def _extract_stack(limit):
    frame = sys_getframe()
    frames = []

    while limit and frame is not None:
        limit -= 1
        frames.append((frame.f_code, frame.f_lineno))
        frame = frame.f_back

    return frames


_greenlet__init__ = greenlet.__init__

class Greenlet(greenlet):
    """
    A light-weight cooperatively-scheduled execution unit.
    """
    # pylint:disable=too-many-public-methods,too-many-instance-attributes

    spawning_stack_limit = 10

    # pylint:disable=keyword-arg-before-vararg,super-init-not-called
    def __init__(self, run=None, *args, **kwargs):
        """
        :param args: The arguments passed to the ``run`` function.
        :param kwargs: The keyword arguments passed to the ``run`` function.
        :keyword callable run: The callable object to run. If not given, this object's
            `_run` method will be invoked (typically defined by subclasses).

        .. versionchanged:: 1.1b1
            The ``run`` argument to the constructor is now verified to be a callable
            object. Previously, passing a non-callable object would fail after the greenlet
            was spawned.

        .. rubric:: Attributes

        .. attribute:: value

            Holds the value returned by the function if the greenlet has
            finished successfully. Until then, or if it finished in error, `None`.

            .. tip:: Recall that a greenlet killed with the default
                     :class:`GreenletExit` is considered to have finished
                     successfully, and the `GreenletExit` exception will be
                     its value.


        .. attribute:: spawn_tree_locals

            A dictionary that is shared between all the greenlets
            in a "spawn tree", that is, a spawning greenlet and all
            its descendent greenlets. All children of the main (root)
            greenlet start their own spawn trees. Assign a new dictionary
            to this attribute on an instance of this class to create a new
            spawn tree (as far as locals are concerned).

            .. versionadded:: 1.3a2

        .. attribute:: spawning_greenlet

            A weak-reference to the greenlet that was current when this object
            was created. Note that the :attr:`parent` attribute is always the
            hub.

            .. versionadded:: 1.3a2

        .. attribute:: spawning_stack

           A lightweight frame-like object capturing the stack when
           this greenlet was created as well as the stack when the spawning
           greenlet was created (if applicable). This can be passed to
           :func:`traceback.print_stack`.

            .. versionadded:: 1.3a2

        .. attribute:: spawning_stack_limit

            A class attribute specifying how many levels of the spawning
            stack will be kept. Specify a smaller number for higher performance,
            spawning greenlets, specify a larger value for improved debugging.

            .. versionadded:: 1.3a2

        .. versionchanged:: 1.3b1
           The ``GEVENT_TRACK_GREENLET_TREE`` configuration value may be set to
           a false value to disable ``spawn_tree_locals``, ``spawning_greenlet``,
           and ``spawning_stack``. The first two will be None in that case, and the
           latter will be empty.
        """
        # greenlet.greenlet(run=None, parent=None)
        # Calling it with both positional arguments instead of a keyword
        # argument (parent=get_hub()) speeds up creation of this object ~30%:
        # python -m timeit -s 'import gevent' 'gevent.Greenlet()'
        # Python 3.5: 2.70usec with keywords vs 1.94usec with positional
        # Python 3.4: 2.32usec with keywords vs 1.74usec with positional
        # Python 3.3: 2.55usec with keywords vs 1.92usec with positional
        # Python 2.7: 1.73usec with keywords vs 1.40usec with positional

        # Timings taken Feb 21 2018 prior to integration of #755
        # python -m perf timeit -s 'import gevent' 'gevent.Greenlet()'
        # 3.6.4       : Mean +- std dev: 1.08 us +- 0.05 us
        # 2.7.14      : Mean +- std dev: 1.44 us +- 0.06 us
        # PyPy2 5.10.0: Mean +- std dev: 2.14 ns +- 0.08 ns

        # After the integration of spawning_stack, spawning_greenlet,
        # and spawn_tree_locals on that same date:
        # 3.6.4       : Mean +- std dev: 8.92 us +- 0.36 us ->  8.2x
        # 2.7.14      : Mean +- std dev: 14.8 us +- 0.5 us  -> 10.2x
        # PyPy2 5.10.0: Mean +- std dev: 3.24 us +- 0.17 us ->  1.5x

        # Compiling with Cython gets us to these numbers:
        # 3.6.4        : Mean +- std dev: 3.63 us +- 0.14 us
        # 2.7.14       : Mean +- std dev: 3.37 us +- 0.20 us
        # PyPy2 5.10.0 : Mean +- std dev: 4.44 us +- 0.28 us


        _greenlet__init__(self, None, get_hub())

        if run is not None:
            self._run = run

        # If they didn't pass a callable at all, then they must
        # already have one. Note that subclassing to override the run() method
        # itself has never been documented or supported.
        if not callable(self._run):
            raise TypeError("The run argument or self._run must be callable")

        self.args = args
        self.kwargs = kwargs
        self.value = None

        #: An event, such as a timer or a callback that fires. It is established in
        #: start() and start_later() as those two objects, respectively.
        #: Once this becomes non-None, the Greenlet cannot be started again. Conversely,
        #: kill() and throw() check for non-None to determine if this object has ever been
        #: scheduled for starting. A placeholder _dummy_event is assigned by them to prevent
        #: the greenlet from being started in the future, if necessary.
        self._start_event = None

        self._notifier = None
        self._formatted_info = None
        self._links = []
        self._ident = None

        # Initial state: None.
        # Completed successfully: (None, None, None)
        # Failed with exception: (t, v, dump_traceback(tb)))
        self._exc_info = None

        if GEVENT_CONFIG.track_greenlet_tree:
            spawner = getcurrent() # pylint:disable=undefined-variable
            self.spawning_greenlet = wref(spawner)
            try:
                self.spawn_tree_locals = spawner.spawn_tree_locals
            except AttributeError:
                self.spawn_tree_locals = {}
                if spawner.parent is not None:
                    # The main greenlet has no parent.
                    # Its children get separate locals.
                    spawner.spawn_tree_locals = self.spawn_tree_locals

            self._spawning_stack_frames = _extract_stack(self.spawning_stack_limit)
            self._spawning_stack_frames.extend(getattr(spawner, '_spawning_stack_frames', []))
        else:
            # None is the default for all of these in Cython, but we
            # need to declare them for pure-Python mode.
            self.spawning_greenlet = None
            self.spawn_tree_locals = None
            self._spawning_stack_frames = None

    @Lazy
    def spawning_stack(self):
        # Store this in the __dict__. We don't use it from the C
        # code. It's tempting to discard _spawning_stack_frames
        # after this, but child greenlets may still be created
        # that need it.
        return _Frame_from_list(self._spawning_stack_frames or [])

    def _get_minimal_ident(self):
        reg = self.parent.ident_registry
        return reg.get_ident(self)

    @property
    def minimal_ident(self):
        """
        A small, unique integer that identifies this object.

        This is similar to :attr:`threading.Thread.ident` (and `id`)
        in that as long as this object is alive, no other greenlet *in
        this hub* will have the same id, but it makes a stronger
        guarantee that the assigned values will be small and
        sequential. Sometime after this object has died, the value
        will be available for reuse.

        To get ids that are unique across all hubs, combine this with
        the hub's ``minimal_ident``.

        .. versionadded:: 1.3a2
        """
        if self._ident is None:
            self._ident = self._get_minimal_ident()
        return self._ident

    @readproperty
    def name(self):
        """
        The greenlet name. By default, a unique name is constructed using
        the :attr:`minimal_ident`. You can assign a string to this
        value to change it. It is shown in the `repr` of this object.

        .. versionadded:: 1.3a2
        """
        return 'Greenlet-%d' % (self.minimal_ident)

    def _raise_exception(self):
        reraise(*self.exc_info)

    @property
    def loop(self):
        # needed by killall
        return self.parent.loop

    def __nonzero__(self):
        return self._start_event is not None and self._exc_info is None
    try:
        __bool__ = __nonzero__ # Python 3
    except NameError: # pragma: no cover
        # When we're compiled with Cython, the __nonzero__ function
        # goes directly into the slot and can't be accessed by name.
        pass

    ### Lifecycle

    if _PYPY:
        # oops - pypy's .dead relies on __nonzero__ which we overriden above
        @property
        def dead(self):
            if self._greenlet__main:
                return False
            if self.__start_cancelled_by_kill() or self.__started_but_aborted():
                return True

            return self._greenlet__started and not _continulet.is_pending(self)
    else:
        @property
        def dead(self):
            return self.__start_cancelled_by_kill() or self.__started_but_aborted() or greenlet.dead.__get__(self)

    def __never_started_or_killed(self):
        return self._start_event is None

    def __start_pending(self):
        return (self._start_event is not None
                and (self._start_event.pending or getattr(self._start_event, 'active', False)))

    def __start_cancelled_by_kill(self):
        return self._start_event is _cancelled_start_event

    def __start_completed(self):
        return self._start_event is _start_completed_event

    def __started_but_aborted(self):
        return (not self.__never_started_or_killed() # we have been started or killed
                and not self.__start_cancelled_by_kill() # we weren't killed, so we must have been started
                and not self.__start_completed() # the start never completed
                and not self.__start_pending()) # and we're not pending, so we must have been aborted

    def __cancel_start(self):
        if self._start_event is None:
            # prevent self from ever being started in the future
            self._start_event = _cancelled_start_event
        # cancel any pending start event
        # NOTE: If this was a real pending start event, this will leave a
        # "dangling" callback/timer object in the hub.loop.callbacks list;
        # depending on where we are in the event loop, it may even be in a local
        # variable copy of that list (in _run_callbacks). This isn't a problem,
        # except for the leak-tests.
        self._start_event.stop()
        self._start_event.close()

    def __handle_death_before_start(self, args):
        # args is (t, v, tb) or simply t or v
        if self._exc_info is None and self.dead:
            # the greenlet was never switched to before and it will never be, _report_error was not called
            # the result was not set and the links weren't notified. let's do it here.
            # checking that self.dead is true is essential, because throw() does not necessarily kill the greenlet
            # (if the exception raised by throw() is caught somewhere inside the greenlet).
            if len(args) == 1:
                arg = args[0]
                #if isinstance(arg, type):
                if type(arg) is type(Exception):
                    args = (arg, arg(), None)
                else:
                    args = (type(arg), arg, None)
            elif not args:
                args = (GreenletExit, GreenletExit(), None)
            self._report_error(args)

    @property
    def started(self):
        # DEPRECATED
        return bool(self)

    def ready(self):
        """
        Return a true value if and only if the greenlet has finished
        execution.

        .. versionchanged:: 1.1
            This function is only guaranteed to return true or false *values*, not
            necessarily the literal constants ``True`` or ``False``.
        """
        return self.dead or self._exc_info is not None

    def successful(self):
        """
        Return a true value if and only if the greenlet has finished execution
        successfully, that is, without raising an error.

        .. tip:: A greenlet that has been killed with the default
            :class:`GreenletExit` exception is considered successful.
            That is, ``GreenletExit`` is not considered an error.

        .. note:: This function is only guaranteed to return true or false *values*,
              not necessarily the literal constants ``True`` or ``False``.
        """
        return self._exc_info is not None and self._exc_info[1] is None

    def __repr__(self):
        classname = self.__class__.__name__
        result = '<%s "%s" at %s' % (classname, self.name, hex(id(self)))
        formatted = self._formatinfo()
        if formatted:
            result += ': ' + formatted
        return result + '>'


    def _formatinfo(self):
        info = self._formatted_info
        if info is not None:
            return info

        # Are we running an arbitrary function provided to the constructor,
        # or did a subclass override _run?
        func = self._run
        im_self = getattr(func, '__self__', None)
        if im_self is self:
            funcname = '_run'
        elif im_self is not None:
            funcname = repr(func)
        else:
            funcname = getattr(func, '__name__', '') or repr(func)

        result = funcname
        args = []
        if self.args:
            args = [repr(x)[:50] for x in self.args]
        if self.kwargs:
            args.extend(['%s=%s' % (key, repr(value)[:50]) for (key, value) in self.kwargs.items()])
        if args:
            result += '(' + ', '.join(args) + ')'
        # it is important to save the result here, because once the greenlet exits '_run' attribute will be removed
        self._formatted_info = result
        return result

    @property
    def exception(self):
        """
        Holds the exception instance raised by the function if the
        greenlet has finished with an error. Otherwise ``None``.
        """
        return self._exc_info[1] if self._exc_info is not None else None

    @property
    def exc_info(self):
        """
        Holds the exc_info three-tuple raised by the function if the
        greenlet finished with an error. Otherwise a false value.

        .. note:: This is a provisional API and may change.

        .. versionadded:: 1.1
        """
        ei = self._exc_info
        if ei is not None and ei[0] is not None:
            return (ei[0], ei[1], load_traceback(ei[2]))

    def throw(self, *args):
        """Immediately switch into the greenlet and raise an exception in it.

        Should only be called from the HUB, otherwise the current greenlet is left unscheduled forever.
        To raise an exception in a safe manner from any greenlet, use :meth:`kill`.

        If a greenlet was started but never switched to yet, then also
        a) cancel the event that will start it
        b) fire the notifications as if an exception was raised in a greenlet
        """
        self.__cancel_start()

        try:
            if not self.dead:
                # Prevent switching into a greenlet *at all* if we had never
                # started it. Usually this is the same thing that happens by throwing,
                # but if this is done from the hub with nothing else running, prevents a
                # LoopExit.
                greenlet.throw(self, *args)
        finally:
            self.__handle_death_before_start(args)

    def start(self):
        """Schedule the greenlet to run in this loop iteration"""
        if self._start_event is None:
            self._start_event = self.parent.loop.run_callback(self.switch)

    def start_later(self, seconds):
        """
        start_later(seconds) -> None

        Schedule the greenlet to run in the future loop iteration
        *seconds* later
        """
        if self._start_event is None:
            self._start_event = self.parent.loop.timer(seconds)
            self._start_event.start(self.switch)

    @classmethod
    def spawn(cls, *args, **kwargs):
        """
        spawn(function, *args, **kwargs) -> Greenlet

        Create a new :class:`Greenlet` object and schedule it to run ``function(*args, **kwargs)``.
        This can be used as ``gevent.spawn`` or ``Greenlet.spawn``.

        The arguments are passed to :meth:`Greenlet.__init__`.

        .. versionchanged:: 1.1b1
            If a *function* is given that is not callable, immediately raise a :exc:`TypeError`
            instead of spawning a greenlet that will raise an uncaught TypeError.
        """
        g = cls(*args, **kwargs)
        g.start()
        return g

    @classmethod
    def spawn_later(cls, seconds, *args, **kwargs):
        """
        spawn_later(seconds, function, *args, **kwargs) -> Greenlet

        Create and return a new `Greenlet` object scheduled to run ``function(*args, **kwargs)``
        in a future loop iteration *seconds* later. This can be used as ``Greenlet.spawn_later``
        or ``gevent.spawn_later``.

        The arguments are passed to :meth:`Greenlet.__init__`.

        .. versionchanged:: 1.1b1
           If an argument that's meant to be a function (the first argument in *args*, or the ``run`` keyword )
           is given to this classmethod (and not a classmethod of a subclass),
           it is verified to be callable. Previously, the spawned greenlet would have failed
           when it started running.
        """
        if cls is Greenlet and not args and 'run' not in kwargs:
            raise TypeError("")
        g = cls(*args, **kwargs)
        g.start_later(seconds)
        return g

    def kill(self, exception=GreenletExit, block=True, timeout=None):
        """
        Raise the ``exception`` in the greenlet.

        If ``block`` is ``True`` (the default), wait until the greenlet dies or the optional timeout expires.
        If block is ``False``, the current greenlet is not unscheduled.

        The function always returns ``None`` and never raises an error.

        .. note::

            Depending on what this greenlet is executing and the state
            of the event loop, the exception may or may not be raised
            immediately when this greenlet resumes execution. It may
            be raised on a subsequent green call, or, if this greenlet
            exits before making such a call, it may not be raised at
            all. As of 1.1, an example where the exception is raised
            later is if this greenlet had called :func:`sleep(0)
            <gevent.sleep>`; an example where the exception is raised
            immediately is if this greenlet had called
            :func:`sleep(0.1) <gevent.sleep>`.

        .. caution::

            Use care when killing greenlets. If the code executing is not
            exception safe (e.g., makes proper use of ``finally``) then an
            unexpected exception could result in corrupted state.

        See also :func:`gevent.kill`.

        :keyword type exception: The type of exception to raise in the greenlet. The default
            is :class:`GreenletExit`, which indicates a :meth:`successful` completion
            of the greenlet.

        .. versionchanged:: 0.13.0
            *block* is now ``True`` by default.
        .. versionchanged:: 1.1a2
            If this greenlet had never been switched to, killing it will prevent it from ever being switched to.
        """
        self.__cancel_start()

        if self.dead:
            self.__handle_death_before_start((exception,))
        else:
            waiter = Waiter() if block else None # pylint:disable=undefined-variable
            self.parent.loop.run_callback(_kill, self, exception, waiter)
            if block:
                waiter.get()
                self.join(timeout)
        # it should be OK to use kill() in finally or kill a greenlet from more than one place;
        # thus it should not raise when the greenlet is already killed (= not started)

    def get(self, block=True, timeout=None):
        """
        get(block=True, timeout=None) -> object

        Return the result the greenlet has returned or re-raise the
        exception it has raised.

        If block is ``False``, raise :class:`gevent.Timeout` if the
        greenlet is still alive. If block is ``True``, unschedule the
        current greenlet until the result is available or the timeout
        expires. In the latter case, :class:`gevent.Timeout` is
        raised.
        """
        if self.ready():
            if self.successful():
                return self.value
            self._raise_exception()
        if not block:
            raise Timeout()

        switch = getcurrent().switch # pylint:disable=undefined-variable
        self.rawlink(switch)
        try:
            t = Timeout._start_new_or_dummy(timeout)
            try:
                result = self.parent.switch()
                if result is not self:
                    raise InvalidSwitchError('Invalid switch into Greenlet.get(): %r' % (result, ))
            finally:
                t.cancel()
        except:
            # unlinking in 'except' instead of finally is an optimization:
            # if switch occurred normally then link was already removed in _notify_links
            # and there's no need to touch the links set.
            # Note, however, that if "Invalid switch" assert was removed and invalid switch
            # did happen, the link would remain, causing another invalid switch later in this greenlet.
            self.unlink(switch)
            raise

        if self.ready():
            if self.successful():
                return self.value
            self._raise_exception()

    def join(self, timeout=None):
        """
        join(timeout=None) -> None

        Wait until the greenlet finishes or *timeout* expires. Return
        ``None`` regardless.
        """
        if self.ready():
            return

        switch = getcurrent().switch # pylint:disable=undefined-variable
        self.rawlink(switch)
        try:
            t = Timeout._start_new_or_dummy(timeout)
            try:
                result = self.parent.switch()
                if result is not self:
                    raise InvalidSwitchError('Invalid switch into Greenlet.join(): %r' % (result, ))
            finally:
                t.cancel()
        except Timeout as ex:
            self.unlink(switch)
            if ex is not t:
                raise
        except:
            self.unlink(switch)
            raise

    def _report_result(self, result):
        self._exc_info = (None, None, None)
        self.value = result
        if self._links and not self._notifier:
            self._notifier = self.parent.loop.run_callback(self._notify_links)

    def _report_error(self, exc_info):
        if isinstance(exc_info[1], GreenletExit):
            self._report_result(exc_info[1])
            return

        self._exc_info = exc_info[0], exc_info[1], dump_traceback(exc_info[2])

        if self._links and not self._notifier:
            self._notifier = self.parent.loop.run_callback(self._notify_links)

        try:
            self.parent.handle_error(self, *exc_info)
        finally:
            del exc_info

    def run(self):
        try:
            self.__cancel_start()
            self._start_event = _start_completed_event

            try:
                result = self._run(*self.args, **self.kwargs)
            except: # pylint:disable=bare-except
                self._report_error(sys_exc_info())
                return
            self._report_result(result)
        finally:
            self.__dict__.pop('_run', None)
            self.args = ()
            self.kwargs.clear()

    def _run(self):
        """
        Subclasses may override this method to take any number of
        arguments and keyword arguments.

        .. versionadded:: 1.1a3
            Previously, if no callable object was
            passed to the constructor, the spawned greenlet would later
            fail with an AttributeError.
        """
        # We usually override this in __init__
        # pylint: disable=method-hidden
        return

    def has_links(self):
        return len(self._links)

    def rawlink(self, callback):
        """
        Register a callable to be executed when the greenlet finishes
        execution.

        The *callback* will be called with this instance as an
        argument.

        .. caution:: The callable will be called in the HUB greenlet.
        """
        if not callable(callback):
            raise TypeError('Expected callable: %r' % (callback, ))
        self._links.append(callback) # pylint:disable=no-member
        if self.ready() and self._links and not self._notifier:
            self._notifier = self.parent.loop.run_callback(self._notify_links)

    def link(self, callback, SpawnedLink=SpawnedLink):
        """
        Link greenlet's completion to a callable.

        The *callback* will be called with this instance as an
        argument once this greenlet is dead. A callable is called in
        its own :class:`greenlet.greenlet` (*not* a
        :class:`Greenlet`).
        """
        # XXX: Is the redefinition of SpawnedLink supposed to just be an
        # optimization, or do people use it? It's not documented
        # pylint:disable=redefined-outer-name
        self.rawlink(SpawnedLink(callback))

    def unlink(self, callback):
        """Remove the callback set by :meth:`link` or :meth:`rawlink`"""
        try:
            self._links.remove(callback) # pylint:disable=no-member
        except ValueError:
            pass

    def unlink_all(self):
        """
        Remove all the callbacks.

        .. versionadded:: 1.3a2
        """
        del self._links[:]

    def link_value(self, callback, SpawnedLink=SuccessSpawnedLink):
        """
        Like :meth:`link` but *callback* is only notified when the greenlet
        has completed successfully.
        """
        # pylint:disable=redefined-outer-name
        self.link(callback, SpawnedLink=SpawnedLink)

    def link_exception(self, callback, SpawnedLink=FailureSpawnedLink):
        """
        Like :meth:`link` but *callback* is only notified when the
        greenlet dies because of an unhandled exception.
        """
        # pylint:disable=redefined-outer-name
        self.link(callback, SpawnedLink=SpawnedLink)

    def _notify_links(self):
        while self._links:
            # Early links are allowed to remove later links
            # before we get to them, and they're also allowed to
            # add new links, so we have to be careful about iterating.

            # We don't expect this list to be very large, so the time spent
            # manipulating it should be small. a deque is probably not justified.
            # Cython has optimizations to transform this into a memmove anyway.
            link = self._links.pop(0)
            try:
                link(self)
            except: # pylint:disable=bare-except
                self.parent.handle_error((link, self), *sys_exc_info())


class _dummy_event(object):
    __slots__ = ('pending', 'active')

    def __init__(self):
        self.pending = self.active = False

    def stop(self):
        pass

    def start(self, cb): # pylint:disable=unused-argument
        raise AssertionError("Cannot start the dummy event")

    def close(self):
        pass

_cancelled_start_event = _dummy_event()
_start_completed_event = _dummy_event()


def _kill(glet, exception, waiter):
    try:
        glet.throw(exception)
    except: # pylint:disable=bare-except
        # XXX do we need this here?
        glet.parent.handle_error(glet, *sys_exc_info())
    if waiter is not None:
        waiter.switch(None)


def joinall(greenlets, timeout=None, raise_error=False, count=None):
    """
    Wait for the ``greenlets`` to finish.

    :param greenlets: A sequence (supporting :func:`len`) of greenlets to wait for.
    :keyword float timeout: If given, the maximum number of seconds to wait.
    :return: A sequence of the greenlets that finished before the timeout (if any)
        expired.
    """
    if not raise_error:
        return wait(greenlets, timeout=timeout, count=count)

    done = []
    for obj in iwait(greenlets, timeout=timeout, count=count):
        if getattr(obj, 'exception', None) is not None:
            if hasattr(obj, '_raise_exception'):
                obj._raise_exception()
            else:
                raise obj.exception
        done.append(obj)
    return done


def _killall3(greenlets, exception, waiter):
    diehards = []
    for g in greenlets:
        if not g.dead:
            try:
                g.throw(exception)
            except: # pylint:disable=bare-except
                g.parent.handle_error(g, *sys_exc_info())
            if not g.dead:
                diehards.append(g)
    waiter.switch(diehards)


def _killall(greenlets, exception):
    for g in greenlets:
        if not g.dead:
            try:
                g.throw(exception)
            except: # pylint:disable=bare-except
                g.parent.handle_error(g, *sys_exc_info())


def killall(greenlets, exception=GreenletExit, block=True, timeout=None):
    """
    Forceably terminate all the ``greenlets`` by causing them to raise ``exception``.

    .. caution:: Use care when killing greenlets. If they are not prepared for exceptions,
       this could result in corrupted state.

    :param greenlets: A **bounded** iterable of the non-None greenlets to terminate.
       *All* the items in this iterable must be greenlets that belong to the same thread.
    :keyword exception: The exception to raise in the greenlets. By default this is
        :class:`GreenletExit`.
    :keyword bool block: If True (the default) then this function only returns when all the
        greenlets are dead; the current greenlet is unscheduled during that process.
        If greenlets ignore the initial exception raised in them,
        then they will be joined (with :func:`gevent.joinall`) and allowed to die naturally.
        If False, this function returns immediately and greenlets will raise
        the exception asynchronously.
    :keyword float timeout: A time in seconds to wait for greenlets to die. If given, it is
        only honored when ``block`` is True.
    :raise Timeout: If blocking and a timeout is given that elapses before
        all the greenlets are dead.

    .. versionchanged:: 1.1a2
        *greenlets* can be any iterable of greenlets, like an iterator or a set.
        Previously it had to be a list or tuple.
    """
    # support non-indexable containers like iterators or set objects
    greenlets = list(greenlets)
    if not greenlets:
        return
    loop = greenlets[0].loop
    if block:
        waiter = Waiter() # pylint:disable=undefined-variable
        loop.run_callback(_killall3, greenlets, exception, waiter)
        t = Timeout._start_new_or_dummy(timeout)
        try:
            alive = waiter.get()
            if alive:
                joinall(alive, raise_error=False)
        finally:
            t.cancel()
    else:
        loop.run_callback(_killall, greenlets, exception)

def _init():
    greenlet_init() # pylint:disable=undefined-variable

_init()

from gevent._util import import_c_accel
import_c_accel(globals(), 'gevent._greenlet')
