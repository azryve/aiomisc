import asyncio
import inspect
from collections import deque
from concurrent.futures import Executor
from types import TracebackType
from typing import (
    Any, AsyncIterator, Awaitable, Callable, Deque, Generator, NoReturn,
    Optional, Type, TypeVar,
)
from weakref import finalize

from aiomisc.compat import EventLoopMixin
from aiomisc.counters import Statistic


T = TypeVar("T")
R = TypeVar("R")

GenType = Generator[T, R, None]
FuncType = Callable[[], GenType]


class ChannelClosed(RuntimeError):
    pass


class FromThreadChannel:
    __slots__ = ("queue", "__closed", "_max_size")

    def __init__(self, maxsize: int):
        self.queue: Deque[Any] = deque()
        self._max_size = maxsize
        self.__closed = False

    def close(self) -> None:
        self.__closed = True

    @property
    def is_overflow(self) -> bool:
        if self._max_size > 0:
            return len(self.queue) >= self._max_size
        return False

    @property
    def is_empty(self) -> bool:
        return len(self.queue) == 0

    @property
    def is_closed(self) -> bool:
        return self.__closed

    def __enter__(self) -> "FromThreadChannel":
        return self

    def __exit__(
        self, exc_type: Type[Exception],
        exc_val: Exception, exc_tb: TracebackType,
    ) -> None:
        self.close()

    def put(self, item: Any) -> None:
        self.queue.append(item)

    def __await__(self) -> Any:
        while self.is_empty and not self.is_closed:
            yield None

        if self.is_closed and self.is_empty:
            raise ChannelClosed

        return self.queue.popleft()


class IteratorWrapperStatistic(Statistic):
    started: int
    queue_size: int
    queue_length: int
    yielded: int
    enqueued: int


class IteratorWrapper(AsyncIterator, EventLoopMixin):
    __slots__ = (
        "__channel",
        "__close_event",
        "__gen_func",
        "__gen_task",
        "_statistic",
        "executor",
    ) + EventLoopMixin.__slots__

    def __init__(
        self, gen_func: FuncType,
        loop: asyncio.AbstractEventLoop = None,
        max_size: int = 0, executor: Executor = None,
        statistic_name: Optional[str] = None,
    ):

        self._loop = loop
        self.executor = executor

        self.__close_event = asyncio.Event()
        self.__channel: FromThreadChannel = FromThreadChannel(maxsize=max_size)
        self.__gen_task: Optional[asyncio.Future] = None
        self.__gen_func: Callable = gen_func
        self._statistic = IteratorWrapperStatistic(statistic_name)
        self._statistic.queue_size = max_size

    @property
    def closed(self) -> bool:
        return self.__channel.is_closed

    @staticmethod
    def __throw(_: Any) -> NoReturn:
        pass

    def _in_thread(self) -> None:
        self._statistic.started += 1
        with self.__channel:
            try:
                gen = iter(self.__gen_func())

                throw = self.__throw
                if inspect.isgenerator(gen):
                    throw = gen.throw   # type: ignore

                while not self.closed:
                    item = next(gen)
                    try:
                        self.__channel.put((item, False))
                    except Exception as e:
                        throw(e)
                        self.__channel.close()
                        break
                    finally:
                        del item

                    self._statistic.enqueued += 1
            except StopIteration:
                return
            except Exception as e:
                if self.closed:
                    return
                self.__channel.put((e, True))
            finally:
                self._statistic.started -= 1
                self.loop.call_soon_threadsafe(self.__close_event.set)

    def close(self) -> Awaitable[None]:
        self.__channel.close()
        return asyncio.ensure_future(self.wait_closed())

    async def wait_closed(self) -> None:
        await self.__close_event.wait()
        if self.__gen_task:
            await asyncio.gather(self.__gen_task, return_exceptions=True)

    def __aiter__(self) -> AsyncIterator[Any]:
        if self.__gen_task is None:
            gen_task = self.loop.run_in_executor(
                self.executor, self._in_thread,
            )
            if gen_task is None:
                raise RuntimeError("Iterator task was not created")
            self.__gen_task = gen_task
        return IteratorProxy(self, self.close)

    async def __anext__(self) -> Awaitable[T]:
        try:
            item, is_exc = await self.__channel
        except ChannelClosed:
            await self.wait_closed()
            raise StopAsyncIteration

        if is_exc:
            await self.close()
            raise item from item

        self._statistic.yielded += 1
        return item

    async def __aenter__(self) -> "IteratorWrapper":
        return self

    async def __aexit__(
        self, exc_type: Any, exc_val: Any,
        exc_tb: Any,
    ) -> None:
        if self.closed:
            return

        await self.close()


class IteratorProxy(AsyncIterator):
    def __init__(
        self, iterator: AsyncIterator,
        finalizer: Callable[[], Any],
    ):
        self.__iterator = iterator
        finalize(self, finalizer)

    def __anext__(self) -> Awaitable[Any]:
        return self.__iterator.__anext__()
