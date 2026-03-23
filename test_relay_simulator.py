"""Tests for the relay simulator caching algorithm."""

import asyncio

from relay_simulator import (
    Cache, CachedObject, Consumer, RelaySubscriptionHandler, Subscription,
)


class MockUpstream:
    def __init__(self) -> None:
        self._objects: dict[int, list[CachedObject]] = {}
        self._subscribe_delay: float = 0.01

    def add_object(self, group_id: int, subgroup_id: int,
                   object_id: int, payload: bytes | None = None) -> None:
        if payload is None:
            payload = f"g{group_id}sg{subgroup_id}o{object_id}".encode()
        obj = CachedObject(group_id, subgroup_id, object_id, payload)
        self._objects.setdefault(group_id, []).append(obj)
        self._objects[group_id].sort(key=lambda o: o.object_id)

    async def subscribe(self, consumer: Consumer) -> None:
        for group_id in sorted(self._objects):
            for obj in self._objects[group_id]:
                await asyncio.sleep(self._subscribe_delay)
                consumer.publish_object(
                    obj.group_id, obj.subgroup_id,
                    obj.object_id, obj.payload)

    async def fetch(self, group: int, start: int, end: int,
                    consumer: Consumer) -> None:
        for obj in self._objects.get(group, []):
            if start <= obj.object_id < end:
                consumer.publish_object(
                    obj.group_id, obj.subgroup_id,
                    obj.object_id, obj.payload)


class RecordingConsumer:
    def __init__(self) -> None:
        self.received: list[tuple[int, int, int, bytes]] = []

    def publish_object(self, group_id: int, subgroup_id: int,
                       object_id: int, payload: bytes) -> None:
        self.received.append((group_id, subgroup_id, object_id, payload))


async def test_complete_cache():
    """No gaps. Emitters run straight through, transition to live."""
    cache = Cache()
    upstream = MockUpstream()
    consumer = RecordingConsumer()

    # Pre-populate cache: group 0, sg 0, objects 0-3
    for i in range(4):
        cache.put(CachedObject(0, 0, i, f"obj{i}".encode()))
    group = cache.get_group(0)
    group.known_objects.add(0, 4)
    group.total_objects = 4
    sg = group.subgroups[0]
    sg.try_advance(group.known_objects)
    assert sg.largest_contiguous == 3

    sub = Subscription(cache, upstream)
    handler = RelaySubscriptionHandler(sub, consumer, 0)
    await handler.run()

    assert len(consumer.received) == 4
    for i in range(4):
        assert consumer.received[i] == (0, 0, i, f"obj{i}".encode())
    print("  PASS: test_complete_cache")


async def test_gap_with_objects():
    """Gap contains objects for existing and new subgroups."""
    cache = Cache()
    upstream = MockUpstream()
    consumer = RecordingConsumer()

    # Cache: sg0 has obj 0,1 and obj 4,5; gap at 2,3
    for i in [0, 1, 4, 5]:
        cache.put(CachedObject(0, 0, i, f"sg0o{i}".encode()))
    group = cache.get_group(0)
    group.known_objects.add(0, 2)
    group.known_objects.add(4, 6)
    group.total_objects = 6

    sg0 = group.subgroups[0]
    sg0.try_advance(group.known_objects)
    assert sg0.largest_contiguous == 1

    # Upstream has obj 2 (sg0) and obj 3 (sg1 - new subgroup)
    upstream.add_object(0, 0, 2, b"sg0o2")
    upstream.add_object(0, 1, 3, b"sg1o3")

    sub = Subscription(cache, upstream)
    handler = RelaySubscriptionHandler(sub, consumer, 0)
    await handler.run()

    # sg0 should have emitted 0,1,2,4,5; sg1 should have emitted 3
    sg0_objs = [(g, s, o, p) for g, s, o, p in consumer.received if s == 0]
    sg1_objs = [(g, s, o, p) for g, s, o, p in consumer.received if s == 1]
    assert [o for _, _, o, _ in sg0_objs] == [0, 1, 2, 4, 5]
    assert [o for _, _, o, _ in sg1_objs] == [3]
    print("  PASS: test_gap_with_objects")


async def test_gap_with_no_objects():
    """Empty gap — emitters unblock, nothing new to send."""
    cache = Cache()
    upstream = MockUpstream()
    consumer = RecordingConsumer()

    # sg0 has obj 0 and obj 2; gap at 1 (but nothing exists there)
    cache.put(CachedObject(0, 0, 0, b"o0"))
    cache.put(CachedObject(0, 0, 2, b"o2"))
    group = cache.get_group(0)
    group.known_objects.add(0, 1)
    group.known_objects.add(2, 3)
    group.total_objects = 3

    sg = group.subgroups[0]
    sg.try_advance(group.known_objects)
    assert sg.largest_contiguous == 0  # can't advance past 0 yet

    # Upstream has nothing in the gap [1,2)
    sub = Subscription(cache, upstream)
    handler = RelaySubscriptionHandler(sub, consumer, 0)
    await handler.run()

    assert [o for _, _, o, _ in consumer.received] == [0, 2]
    print("  PASS: test_gap_with_no_objects")


async def test_largest_contiguous_negative():
    """Subgroup initially lc=-1 gets resolved by gap fetch, then fills."""
    cache = Cache()
    upstream = MockUpstream()
    consumer = RecordingConsumer()

    # sg0 has obj 0,1 (contiguous); sg1 has only obj 5 (not from start)
    for i in [0, 1]:
        cache.put(CachedObject(0, 0, i, f"sg0o{i}".encode()))
    cache.put(CachedObject(0, 1, 5, b"sg1o5"))
    group = cache.get_group(0)
    group.known_objects.add(0, 2)
    group.known_objects.add(5, 6)
    group.total_objects = 6

    for sg in group.subgroups.values():
        sg.try_advance(group.known_objects)

    assert group.subgroups[0].largest_contiguous == 1
    assert group.subgroups[1].largest_contiguous == -1  # initially not contiguous

    # Upstream delivers sg1 obj 7 via subscribe
    upstream.add_object(0, 1, 7, b"sg1o7_live")

    sub = Subscription(cache, upstream)
    handler = RelaySubscriptionHandler(sub, consumer, 0)
    await handler.run()

    # sg0 filled from cache; sg1 obj 5 filled after gap resolution,
    # obj 7 came through live
    sg0 = [(s, o) for _, s, o, _ in consumer.received if s == 0]
    sg1 = [(s, o) for _, s, o, _ in consumer.received if s == 1]
    assert [o for _, o in sg0] == [0, 1]
    assert [o for _, o in sg1] == [5, 7]
    print("  PASS: test_largest_contiguous_negative")


async def test_independent_progress():
    """sg0 has no gaps (runs immediately), sg1 blocks on gap fetch."""
    cache = Cache()
    upstream = MockUpstream()
    consumer = RecordingConsumer()

    # sg0: objects 0,1,2 fully cached
    for i in range(3):
        cache.put(CachedObject(0, 0, i, f"sg0o{i}".encode()))
    # sg1: objects 0,4 cached; gap at 3
    cache.put(CachedObject(0, 1, 0, b"sg1o0"))
    cache.put(CachedObject(0, 1, 4, b"sg1o4"))

    group = cache.get_group(0)
    group.known_objects.add(0, 3)  # obj 0,1,2 known
    group.known_objects.add(4, 5)  # obj 4 known
    group.total_objects = 5        # scan range [0,5)

    for sg in group.subgroups.values():
        sg.try_advance(group.known_objects)

    assert group.subgroups[0].largest_contiguous == 2  # fully contiguous
    assert group.subgroups[1].largest_contiguous == 0  # blocked after 0

    # Upstream: gap [3,4) has no objects
    sub = Subscription(cache, upstream)
    handler = RelaySubscriptionHandler(sub, consumer, 0)
    await handler.run()

    sg0 = [o for _, s, o, _ in consumer.received if s == 0]
    sg1 = [o for _, s, o, _ in consumer.received if s == 1]
    assert sg0 == [0, 1, 2]
    assert sg1 == [0, 4]
    print("  PASS: test_independent_progress")


async def test_contiguous_advance():
    """sg has {0,2,4,8}, lc=4. Gap fetch finds obj 6 for sg. lc->8."""
    cache = Cache()
    upstream = MockUpstream()
    consumer = RecordingConsumer()

    for oid in [0, 2, 4, 8]:
        cache.put(CachedObject(0, 0, oid, f"o{oid}".encode()))
    group = cache.get_group(0)
    # Mark 0-5 as known (so 0,2,4 are contiguous through known gaps)
    group.known_objects.add(0, 5)
    group.known_objects.add(8, 9)
    group.total_objects = 9

    sg = group.subgroups[0]
    sg.try_advance(group.known_objects)
    assert sg.largest_contiguous == 4

    # Gap [5,8): upstream has obj 6 for sg0
    upstream.add_object(0, 0, 6, b"o6")

    sub = Subscription(cache, upstream)
    handler = RelaySubscriptionHandler(sub, consumer, 0)
    await handler.run()

    objs = [o for _, _, o, _ in consumer.received]
    assert objs == [0, 2, 4, 6, 8]
    # Verify largest_contiguous advanced to 8
    assert sg.largest_contiguous == 8
    print("  PASS: test_contiguous_advance")


async def test_live_transition():
    """Fill completes, filter set to LIVE, next upstream object passes."""
    cache = Cache()
    upstream = MockUpstream()
    consumer = RecordingConsumer()

    # Cache sg0 obj 0
    cache.put(CachedObject(0, 0, 0, b"cached"))
    group = cache.get_group(0)
    group.known_objects.add(0, 1)
    group.total_objects = 1
    group.subgroups[0].try_advance(group.known_objects)

    # Upstream will deliver sg0 obj 1 via subscribe (live)
    upstream.add_object(0, 0, 1, b"live")

    sub = Subscription(cache, upstream)
    handler = RelaySubscriptionHandler(sub, consumer, 0)
    await handler.run()

    # Should get cached obj then live obj, no gap, no duplicate
    assert consumer.received == [
        (0, 0, 0, b"cached"),
        (0, 0, 1, b"live"),
    ]
    print("  PASS: test_live_transition")


async def test_new_subgroup_from_live():
    """Unknown subgroup from upstream subscribe forwarded directly."""
    cache = Cache()
    upstream = MockUpstream()
    consumer = RecordingConsumer()

    # Cache sg0 obj 0
    cache.put(CachedObject(0, 0, 0, b"sg0o0"))
    group = cache.get_group(0)
    group.known_objects.add(0, 1)
    group.total_objects = 1
    group.subgroups[0].try_advance(group.known_objects)

    # Upstream delivers sg2 (brand new) via subscribe
    upstream.add_object(0, 2, 0, b"sg2o0_live")

    sub = Subscription(cache, upstream)
    handler = RelaySubscriptionHandler(sub, consumer, 0)
    await handler.run()

    sg0 = [(s, o, p) for _, s, o, p in consumer.received if s == 0]
    sg2 = [(s, o, p) for _, s, o, p in consumer.received if s == 2]
    assert sg0 == [(0, 0, b"sg0o0")]
    assert sg2 == [(2, 0, b"sg2o0_live")]
    print("  PASS: test_new_subgroup_from_live")


async def test_multi_group_fill():
    """Older groups entirely filled, join group per-subgroup gated."""
    cache = Cache()
    upstream = MockUpstream()
    consumer = RecordingConsumer()

    # Group 0 (older): sg0 obj 0,1 fully cached
    for i in range(2):
        cache.put(CachedObject(0, 0, i, f"g0o{i}".encode()))
    g0 = cache.get_group(0)
    g0.known_objects.add(0, 2)
    g0.total_objects = 2
    g0.subgroups[0].try_advance(g0.known_objects)

    # Group 1 (join group): sg0 obj 0 cached, sg1 not in cache
    cache.put(CachedObject(1, 0, 0, b"g1sg0o0"))
    g1 = cache.get_group(1)
    g1.known_objects.add(0, 1)
    g1.total_objects = 1
    g1.subgroups[0].try_advance(g1.known_objects)

    # Upstream subscribe delivers: g0 sg0 obj 2 (live, older group),
    # g1 sg0 obj 1 (live, join group), g1 sg1 obj 0 (new sg, live)
    upstream.add_object(0, 0, 2, b"g0o2_live")
    upstream.add_object(1, 0, 1, b"g1sg0o1_live")
    upstream.add_object(1, 1, 0, b"g1sg1o0_live")

    sub = Subscription(cache, upstream)
    handler = RelaySubscriptionHandler(sub, consumer, 1, num_fill_groups=1)
    await handler.run()

    # Group 0: filled from cache (obj 0,1). Live obj 2 is dropped
    # by filter (group < join_group) — it wasn't in cache.
    g0_objs = [(o, p) for g, s, o, p in consumer.received if g == 0]
    assert g0_objs == [(0, b"g0o0"), (1, b"g0o1")]

    # Group 1 sg0: filled obj 0 from cache, then live obj 1
    g1sg0 = [(o, p) for g, s, o, p in consumer.received if g == 1 and s == 0]
    assert g1sg0 == [(0, b"g1sg0o0"), (1, b"g1sg0o1_live")]

    # Group 1 sg1: unknown subgroup, passed through live directly
    g1sg1 = [(o, p) for g, s, o, p in consumer.received if g == 1 and s == 1]
    assert g1sg1 == [(0, b"g1sg1o0_live")]

    print("  PASS: test_multi_group_fill")


async def run_all_tests():
    tests = [
        test_complete_cache,
        test_gap_with_objects,
        test_gap_with_no_objects,
        test_largest_contiguous_negative,
        test_independent_progress,
        test_contiguous_advance,
        test_live_transition,
        test_new_subgroup_from_live,
        test_multi_group_fill,
    ]
    print(f"Running {len(tests)} tests...")
    for test in tests:
        await test()
    print(f"All {len(tests)} tests passed!")


if __name__ == "__main__":
    asyncio.run(run_all_tests())
