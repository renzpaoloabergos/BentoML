from __future__ import annotations

import typing as t
import logging
import itertools
from typing import TYPE_CHECKING

from bentoml.exceptions import InvalidArgument

logger = logging.getLogger(__name__)


if TYPE_CHECKING:
    from aiohttp import MultipartWriter
    from starlette.requests import Request

    from ..runner.container import Payload


T = t.TypeVar("T")
To = t.TypeVar("To")


CUDA_SUCCESS = 0


def pass_through(i: T) -> T:
    return i


class Params(t.Generic[T]):
    """
    A container for */** parameters. It helps to perform an operation on all the params
    values at the same time.
    """

    args: tuple[T, ...]
    kwargs: dict[str, T]

    def __init__(
        self,
        *args: T,
        **kwargs: T,
    ):
        self.args = args
        self.kwargs = kwargs

    def items(self) -> t.Iterator[t.Tuple[t.Union[int, str], T]]:
        return itertools.chain(enumerate(self.args), self.kwargs.items())

    @classmethod
    def from_dict(cls, data: dict[str | int, T]) -> Params[T]:
        return cls(
            *(data[k] for k in sorted(k for k in data if isinstance(k, int))),
            **{k: v for k, v in data.items() if isinstance(k, str)},
        )

    def all_equal(self) -> bool:
        value_iter = iter(self.items())
        _, first = next(value_iter)
        return all(v == first for _, v in value_iter)

    def map(self, function: t.Callable[[T], To]) -> Params[To]:
        """
        Apply a function to all the values in the Params and return a Params of the
        return values.
        """
        args = tuple(function(a) for a in self.args)
        kwargs = {k: function(v) for k, v in self.kwargs.items()}
        return Params[To](*args, **kwargs)

    def iter(self: Params[tuple[t.Any, ...]]) -> t.Iterator[Params[t.Any]]:
        """
        Iter over a Params of iterable values into a list of Params. All values should
        have the same length.
        """

        iter_params = self.map(iter)
        try:
            while True:
                args = tuple(next(a) for a in iter_params.args)
                kwargs = {k: next(v) for k, v in iter_params.kwargs.items()}
                yield Params[To](*args, **kwargs)
        except StopIteration:
            pass

    @classmethod
    def agg(
        cls,
        params_list: t.Sequence[Params[T]],
        agg_func: t.Callable[[t.Sequence[T]], To] = pass_through,
    ) -> Params[To]:
        """
        Aggregate a list of Params into a single Params by performing the aggregate
        function on the list of values at the same position.
        """
        if not params_list:
            return Params()

        args = tuple(
            agg_func(tuple(params.args[i] for params in params_list))
            for i, _ in enumerate(params_list[0].args)
        )
        kwargs = {
            k: agg_func(tuple(params.kwargs[k] for params in params_list))
            for k in params_list[0].kwargs
        }
        return Params(*args, **kwargs)

    @property
    def sample(self) -> T:
        """
        Return a sample value (the first value of args or kwargs if args is empty)
        of the Params.
        """
        if self.args:
            return self.args[0]
        return next(iter(self.kwargs.values()))


PAYLOAD_META_HEADER = "Bento-Payload-Meta"


def payload_paramss_to_batch_params(
    paramss: t.Sequence[Params[Payload]],
    batch_dim: int,
    # TODO: support mapping from arg to batch dimension
) -> tuple[Params[t.Any], list[int]]:
    from ..runner.container import AutoContainer

    _converted_params = Params.agg(
        paramss,
        agg_func=lambda i: AutoContainer.from_batch_payloads(
            i,
            batch_dim=batch_dim,
        ),
    ).iter()
    batched_params = next(_converted_params)
    indice_params: Params[list[int]] = next(_converted_params)

    # considering skip this check if the CPU overhead of each inference is too high
    if not indice_params.all_equal():
        raise InvalidArgument(
            f"argument lengths for parameters do not matchs: {tuple(indice_params.items())}"
        )
    return batched_params, indice_params.sample


def payload_params_to_multipart(params: Params[Payload]) -> MultipartWriter:
    import json

    from multidict import CIMultiDict
    from aiohttp.multipart import MultipartWriter

    multipart = MultipartWriter(subtype="form-data")
    for key, payload in params.items():
        multipart.append(
            payload.data,
            headers=CIMultiDict(
                (
                    (PAYLOAD_META_HEADER, json.dumps(payload.meta)),
                    ("Content-Type", f"application/vnd.bentoml.{payload.container}"),
                    ("Content-Disposition", f'form-data; name="{key}"'),
                )
            ),
        )
    return multipart


async def multipart_to_payload_params(request: Request) -> Params[Payload]:
    import json

    from bentoml._internal.runner.container import Payload
    from bentoml._internal.utils.formparser import populate_multipart_requests

    parts = await populate_multipart_requests(request)
    max_arg_index = -1
    kwargs: t.Dict[str, Payload] = {}
    args_map: t.Dict[int, Payload] = {}
    for field_name, req in parts.items():
        payload = Payload(
            data=await req.body(),
            meta=json.loads(req.headers[PAYLOAD_META_HEADER]),
            container=req.headers["Content-Type"].strip("application/vnd.bentoml."),
        )
        if field_name.isdigit():
            arg_index = int(field_name)
            args_map[arg_index] = payload
            max_arg_index = max(max_arg_index, arg_index)
        else:
            kwargs[field_name] = payload
    args = tuple(args_map[i] for i in range(max_arg_index + 1))
    return Params(*args, **kwargs)
