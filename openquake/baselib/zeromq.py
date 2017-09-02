import multiprocessing
import functools
import threading
import zmq
from openquake.baselib.python3compat import pickle
from openquake.baselib.parallel import safely_call

REQ = zmq.REQ
REP = zmq.REP
PUSH = zmq.PUSH
PULL = zmq.PULL
ROUTER = zmq.ROUTER
DEALER = zmq.DEALER
PUB = zmq.PUB
SUB = zmq.SUB
POLLIN = zmq.POLLIN
POLLOUT = zmq.POLLOUT


class _Context(zmq.Context):
    """
    A zmq Context subclass with methods .bind and .connect
    """
    def bind(self, end_point, socket_type, **kw):
        identity = kw.pop('identity') if 'identity' in kw else None
        socket = self.socket(socket_type, **kw)
        if identity:
            socket.identity = identity
        try:
            socket.bind(end_point)
        except Exception as exc:  # invalid end_point
            socket.close()
            raise exc.__class__('%s: %s' % (exc, end_point))
        return socket

    def connect(self, end_point, socket_type, **kw):
        identity = kw.pop('identity') if 'identity' in kw else None
        socket = self.socket(socket_type, **kw)
        if identity:
            socket.identity = identity
        try:
            socket.connect(end_point)
        except Exception as exc:  # invalid end_point
            socket.close()
            raise exc.__class__('%s: %s' % (exc, end_point))
        return socket

# NB: using a global context is probably good: http://250bpm.com/blog:23
context = _Context()


class Process(multiprocessing.Process):
    """
    Process with a zmq socket
    """
    def __init__(self, func, *args, **kw):
        def newfunc(*args, **kw):
            # the only reason it is not .instance() is that there may be a
            # stale Context instance already initialized, from the docs
            with context:
                func(*args, **kw)
        super(Process, self).__init__(target=newfunc, args=args, kwargs=kw)

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, etype, exc, tb):
        self.join()


class Thread(threading.Thread):
    """
    Thread with a zmq socket
    """
    def __init__(self, func, *args, **kw):
        def newfunc(*args, **kw):
            try:
                func(*args, **kw)
            except zmq.ContextTerminated:  # CTRL-C was given
                pass
        super(Thread, self).__init__(target=newfunc, args=args, kwargs=kw)

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, etype, exc, tb):
        self.join()


def proxy(frontend_url, backend_url):
    """
    A zmq proxy routing messages from the frontend to the backend and back
    """
    with context.bind(frontend_url, ROUTER) as frontend, \
            context.bind(backend_url, DEALER) as backend:
        zmq.proxy(frontend, backend)


def master(backend_url, func=None):
    """
    A worker reading tuples and returning results to the backend via a zmq
    socket.

    :param backend_url: URL where to connect
    :param func: if None, expects message to be pairs (cmd, args) else args
    """
    socket = context.connect(backend_url, DEALER)
    while True:
        ident, pik = socket.recv_multipart()
        if func is None:  # retrieve the cmd from the message
            cmd, args = pickle.loads(pik)
        else:  # use the provided func as cmd
            cmd, args = func, pickle.loads(pik)
        if cmd == 'stop':
            print('Received stop command')
            pool.terminate()
            break
        # passing a responder to safely_call, since passing a callback to
        # apply_async fails randomly with BrokenPipeErrors
        resp = Responder(backend_url, DEALER, ident)
        pool.apply_async(safely_call, (cmd, args, resp))


class Responder(object):
    def __init__(self, backend_url, socket_type, ident):
        self.backend_url = backend_url
        self.socket_type = socket_type
        self.ident = ident

    def __enter__(self):
        self.socket = context.connect(self.backend_url, self.socket_type)
        return self

    def __exit__(self, *args):
        self.socket.close()
        del self.socket

    def __call__(self, res):
        self.socket.send_multipart([self.ident, pickle.dumps(res)])


def starmap(frontend_url, func, allargs):
    """
    starmap a function over an iterator of arguments by using a zmq socket
    """
    with context.connect(frontend_url, DEALER) as socket:
        n = 0
        for args in allargs:
            socket.send_pyobj((func, args))
            n += 1
        yield n
        for _ in range(n):
            yield socket.recv_pyobj()


if __name__ == '__main__':  # run workers
    import sys
    try:
        url, _ncores = sys.argv[1:]
        ncores = int(_ncores)
    except ValueError:
        url = sys.argv[1]
        ncores = multiprocessing.cpu_count()
    pool = multiprocessing.Pool(ncores)
    with context:
        master(url)
