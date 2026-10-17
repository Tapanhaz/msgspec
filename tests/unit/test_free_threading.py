"""Multi-threaded stress tests for free-threaded (no-GIL) builds.

Scale the work with MSGSPEC_STRESS_SCALE (default 1).
"""

from __future__ import annotations

import itertools
import os
import threading
from typing import Literal, Union

import pytest

import msgspec
from msgspec import Struct

from .utils import temp_module

if not os.environ.get("MSGSPEC_STRESS"):
    pytest.skip("stress tests: set MSGSPEC_STRESS=1 to run", allow_module_level=True)

pytestmark = pytest.mark.parallel_threads(1)

SCALE = max(1, int(os.environ.get("MSGSPEC_STRESS_SCALE", "1")))
N_THREADS = 8

_unique = itertools.count(10_000_000)


@pytest.fixture(params=["json", "msgpack"])
def proto(request):
    return msgspec.json if request.param == "json" else msgspec.msgpack


def run_threads(fn, n=N_THREADS):
    barrier = threading.Barrier(n)
    errors = []

    def worker(i):
        try:
            barrier.wait(timeout=60)
            fn(i)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    if errors:
        raise errors[0]


def run_with_mutator(mutate, work, n=4):
    stop = threading.Event()

    def mutator():
        while not stop.is_set():
            mutate()

    t = threading.Thread(target=mutator)
    t.start()
    try:
        run_threads(work, n=n)
    finally:
        stop.set()
        t.join()


def test_literal_cache_first_use(proto):
    for _ in range(40 * SCALE):
        base = next(_unique)
        values = (base, base + 1, f"s{base}")
        typ = Literal[values]
        msgs = [(v, proto.encode(v)) for v in values]
        bad = proto.encode(-1)

        def work(i, typ=typ, msgs=msgs, bad=bad):
            dec = proto.Decoder(typ)
            for v, msg in msgs:
                assert dec.decode(msg) == v
            try:
                dec.decode(bad)
            except msgspec.ValidationError:
                pass
            else:
                raise AssertionError("expected ValidationError")

        run_threads(work)


def test_struct_type_first_use(proto):
    for _ in range(40 * SCALE):

        class Fresh(Struct):
            a: int
            b: list[str]

        obj = Fresh(1, ["x", "y"])
        msg = proto.encode(obj)

        def work(i, Fresh=Fresh, obj=obj, msg=msg):
            dec = proto.Decoder(Fresh)
            assert dec.decode(msg) == obj
            assert proto.decode(msg, type=Fresh) == obj

        run_threads(work)


class HotA(Struct, tag=True):
    a: int


class HotB(Struct, tag=True):
    b: int


def test_struct_union_cache_contention(proto):
    hot = Union[HotA, HotB]
    hot_msgs = [(HotA(1), proto.encode(HotA(1))), (HotB(2), proto.encode(HotB(2)))]

    def work(i):
        for _ in range(25 * SCALE):

            class A(Struct, tag=True):
                a: int

            class B(Struct, tag=True):
                b: int

            dec = proto.Decoder(Union[A, B])
            assert dec.decode(proto.encode(A(1))) == A(1)
            assert dec.decode(proto.encode(B(2))) == B(2)

            for obj, msg in hot_msgs:
                assert proto.decode(msg, type=hot) == obj

    run_threads(work)

    from msgspec._core import _struct_lookup_cache as cache

    assert len(cache) <= 64, f"struct lookup cache grew to {len(cache)}"


@pytest.mark.parametrize("array_like", [False, True])
def test_struct_union_first_use_shared_types(proto, array_like):
    for _ in range(40 * SCALE):

        class A(Struct, tag=True, array_like=array_like):
            a: int

        class B(Struct, tag=True, array_like=array_like):
            b: int

        typ = Union[A, B]
        msgs = [(A(1), proto.encode(A(1))), (B(2), proto.encode(B(2)))]

        def work(i, typ=typ, msgs=msgs):
            for obj, msg in msgs:
                assert proto.decode(msg, type=typ) == obj

        run_threads(work)


class Point(Struct):
    x: int
    y: list[int]
    name: str


def test_shared_decoder(proto):
    dec = proto.Decoder(list[Point])

    def work(i):
        pts = [Point(i * j, [i, j], f"n{i}-{j}") for j in range(20)]
        msg = proto.encode(pts)
        for _ in range(150 * SCALE):
            assert dec.decode(msg) == pts

    run_threads(work)


def test_shared_encoder(proto):
    enc = proto.Encoder()

    def work(i):
        obj = {"i": i, "items": [{"k": j, "s": "x" * (i + j)} for j in range(20)]}
        expected = proto.encode(obj)
        for _ in range(150 * SCALE):
            assert enc.encode(obj) == expected

    run_threads(work)


def test_shared_encoder_encode_into(proto):
    enc = proto.Encoder()

    def work(i):
        obj = {"i": i, "s": "y" * (10 + i)}
        expected = proto.encode(obj)
        buf = bytearray()  # per-thread buffer, shared encoder
        for _ in range(150 * SCALE):
            enc.encode_into(obj, buf)
            assert bytes(buf) == expected

    run_threads(work)


def test_encode_list_mutated_concurrently(proto):
    lst = []
    state = {"n": 0}

    def mutate():
        state["n"] += 1
        lst[:] = [f"item-{state['n']}-{k}" for k in range(50 + state["n"] % 150)]
        lst.clear()

    def work(i):
        for _ in range(300 * SCALE):
            res = proto.decode(proto.encode(lst))
            assert isinstance(res, list)
            assert all(isinstance(x, str) for x in res)

    run_with_mutator(mutate, work)


def test_encode_dict_mutated_concurrently(proto):
    d = {}
    state = {"n": 0}

    def mutate():
        state["n"] += 1
        d.update({f"k{k}": f"v{state['n']}-{k}" for k in range(50 + state["n"] % 150)})
        d.clear()

    def work(i):
        for _ in range(300 * SCALE):
            res = proto.decode(proto.encode(d))
            assert isinstance(res, dict)
            assert all(isinstance(v, str) for v in res.values())

    run_with_mutator(mutate, work)


class Box(Struct):
    s: str
    items: list


def test_encode_struct_field_replaced_concurrently(proto):
    box = Box("a", [1, 2, 3])
    state = {"n": 0}

    def mutate():
        state["n"] += 1
        n = state["n"]
        box.s = f"value-{n}" * 4
        box.items = [f"i{n}-{k}" for k in range(1 + n % 8)]

    def work(i):
        for _ in range(300 * SCALE):
            res = proto.decode(proto.encode(box), type=Box)
            assert isinstance(res.s, str)
            assert isinstance(res.items, list)

    run_with_mutator(mutate, work)


def test_concurrent_struct_definition(proto):
    def work(i):
        for k in range(30 * SCALE):

            class S(Struct, array_like=bool(k % 2), kw_only=bool(k % 3 == 0)):
                a: int
                b: str = "x"

            D = msgspec.defstruct(f"D{i}_{k}", [("a", int), ("b", str)], frozen=True)

            for cls in (S, D):
                obj = cls(a=i, b="y")
                assert proto.decode(proto.encode(obj), type=cls) == obj

    run_threads(work)


def test_json_encode_lines_list_mutated_concurrently():
    enc = msgspec.json.Encoder()
    lst = []
    state = {"n": 0}

    def mutate():
        state["n"] += 1
        lst[:] = [f"item-{state['n']}-{k}" for k in range(50 + state["n"] % 150)]
        lst.clear()

    def work(i):
        for _ in range(300 * SCALE):
            out = enc.encode_lines(lst)
            for line in out.splitlines():
                assert isinstance(msgspec.json.decode(line), str)

    run_with_mutator(mutate, work)


def test_to_builtins_list_mutated_concurrently():
    lst = []
    state = {"n": 0}

    def mutate():
        state["n"] += 1
        lst[:] = [f"item-{state['n']}-{k}" for k in range(50 + state["n"] % 150)]
        lst.clear()

    def work(i):
        for _ in range(300 * SCALE):
            res = msgspec.to_builtins(lst)
            assert isinstance(res, list)
            assert all(isinstance(x, str) for x in res)

    run_with_mutator(mutate, work)


def test_convert_list_mutated_concurrently():
    lst = []
    state = {"n": 0}

    def mutate():
        state["n"] += 1
        lst[:] = [f"item-{state['n']}-{k}" for k in range(50 + state["n"] % 150)]
        lst.clear()

    def work(i):
        for _ in range(300 * SCALE):
            res = msgspec.convert(lst, list[str])
            assert isinstance(res, list)
            assert all(isinstance(x, str) for x in res)

    run_with_mutator(mutate, work)


def test_encode_nested_dict_replaced_concurrently(proto):
    outer = {"inner": {}}
    state = {"n": 0}

    def mutate():
        state["n"] += 1
        n = state["n"]
        outer["inner"] = {f"k{k}": f"v{n}-{k}" for k in range(20 + n % 60)}

    def work(i):
        for _ in range(300 * SCALE):
            res = proto.decode(proto.encode(outer))
            assert isinstance(res["inner"], dict)
            assert all(isinstance(v, str) for v in res["inner"].values())

    run_with_mutator(mutate, work)


# --------------------------------------------------------------------------
# Failed conversions of mutually-recursive types
#
# Known bug (reproduces on GIL builds too, single-threaded): converting ``A``
# publishes ``A``'s half-built type info before its fields are processed, so
# converting the mutually-recursive ``B`` inside that call captures it. When
# ``A`` then fails, only ``A``'s cache entry is rolled back: ``B``'s completed
# info stays cached and still references the half-built ``A`` info. A later
# ``Decoder(B)`` wrongly succeeds, and decoding data that reaches ``A`` can
# crash (NULL TypeNode) or, on free-threaded builds, spin forever waiting for
# ``initialized``.
# --------------------------------------------------------------------------

KNOWN_BUG = pytest.mark.xfail(
    strict=True,
    reason="failed conversion of a recursive type leaves a half-built info "
    "cached on the types that reference it",
)

_CYCLE_SOURCES = {
    "struct": """
from __future__ import annotations
from typing import Literal, Optional
from msgspec import Struct

class A(Struct):
    b: Optional[B] = None
    bad: Literal[1.5] = 1.5  # < -- invalid Literal: converting A always fails

class B(Struct):
    a: Optional[A] = None
""",
    "dataclass": """
from __future__ import annotations
from dataclasses import dataclass
from typing import Literal, Optional

@dataclass
class A:
    b: Optional[B] = None
    bad: Literal[1.5] = 1.5

@dataclass
class B:
    a: Optional[A] = None
""",
    "typeddict": """
from __future__ import annotations
from typing import Literal, Optional, TypedDict

class A(TypedDict, total=False):
    b: Optional[B]
    bad: Literal[1.5]

class B(TypedDict, total=False):
    a: Optional[A]
""",
    "namedtuple": """
from __future__ import annotations
from typing import Literal, NamedTuple, Optional

class A(NamedTuple):
    b: Optional[B] = None
    bad: Literal[1.5] = 1.5

class B(NamedTuple):
    a: Optional[A] = None
""",
}


@KNOWN_BUG
@pytest.mark.parametrize("kind", list(_CYCLE_SOURCES))
def test_failed_recursive_conversion_single_thread(kind, proto):
    with temp_module(_CYCLE_SOURCES[kind]) as mod:
        with pytest.raises(TypeError):
            proto.Decoder(mod.A)
        with pytest.raises(TypeError):
            proto.Decoder(mod.B)


@KNOWN_BUG
def test_failed_recursive_conversion_threaded(proto):
    for _ in range(60 * SCALE):
        with temp_module(_CYCLE_SOURCES["struct"]) as mod:
            outcomes = []

            def work(i, mod=mod, outcomes=outcomes):
                target = mod.A if i % 2 == 0 else mod.B
                try:
                    proto.Decoder(target)
                except TypeError:
                    outcomes.append("TypeError")
                else:
                    outcomes.append(f"built a Decoder for {target.__name__}")

            run_threads(work)
            assert set(outcomes) == {"TypeError"}, outcomes
