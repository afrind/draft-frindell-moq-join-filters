"""Relay simulator for MoQT Join Filter subscribe mode caching algorithm.

Validates that a relay can serve both cached (fill) and live content as
subgroups on a single subscription, proving the algorithm is implementable
before proposing spec text.
"""

import asyncio
import heapq
from bisect import bisect_left
from dataclasses import dataclass, field
from typing import Protocol

from interval_set import IntervalSet


# ---------------------------------------------------------------------------
# Protocols
# ---------------------------------------------------------------------------

class Consumer(Protocol):
    def publish_object(self, group_id: int, subgroup_id: int,
                       object_id: int, payload: bytes) -> None: ...


class Upstream(Protocol):
    async def subscribe(self, consumer: Consumer) -> None: ...
    async def fetch(self, group: int, start: int, end: int,
                    consumer: Consumer) -> None: ...


# ---------------------------------------------------------------------------
# Cache data structures
# ---------------------------------------------------------------------------

@dataclass
class CachedObject:
    group_id: int
    subgroup_id: int
    object_id: int
    payload: bytes


@dataclass
class SubgroupInfo:
    object_ids: list[int] = field(default_factory=list)
    complete: bool = False
    largest_contiguous: int = -1
    _contiguous_idx: int = 0  # index into object_ids past largest_contiguous
    contiguous_advanced: asyncio.Event = field(default_factory=asyncio.Event)

    @property
    def integrity_from_start(self) -> bool:
        return self.largest_contiguous >= 0

    def try_advance(self, known_objects: IntervalSet,
                    resolved_end: int | None = None) -> None:
        """Try to advance largest_contiguous using known_objects.

        resolved_end: upper bound of what was just resolved. If the
        next object we need to reach is beyond this, we can skip —
        the new info can't bridge the gap.
        """
        if self._contiguous_idx >= len(self.object_ids):
            return
        # Early out: if the newly resolved range doesn't reach
        # the next object we need to bridge to, it can't help.
        if resolved_end is not None:
            next_id = self.object_ids[self._contiguous_idx]
            if resolved_end <= self.largest_contiguous + 1:
                return
            if resolved_end < next_id:
                return
        advanced = False
        while self._contiguous_idx < len(self.object_ids):
            next_id = self.object_ids[self._contiguous_idx]
            if known_objects.covers_range(
                    self.largest_contiguous + 1, next_id):
                self.largest_contiguous = next_id
                self._contiguous_idx += 1
                advanced = True
            else:
                break
        if advanced:
            self.contiguous_advanced.set()


@dataclass
class GroupInfo:
    known_objects: IntervalSet = field(default_factory=IntervalSet)
    subgroups: dict[int, SubgroupInfo] = field(default_factory=dict)
    complete: bool = False
    total_objects: int = 0  # upper bound for gap scanning
    fetch_complete: asyncio.Event = field(default_factory=asyncio.Event)
    fetch_in_progress: bool = False
    # Observers called with (sg_id) when a new subgroup becomes contiguous
    start_subgroup: list = field(default_factory=list)
    # Index of subgroups blocked on advancing: next_needed_obj_id -> {sg_ids}
    _pending_advance: dict[int, set[int]] = field(default_factory=dict)

    def add_pending(self, sg_id: int, next_obj_id: int) -> None:
        self._pending_advance.setdefault(next_obj_id, set()).add(sg_id)


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------

class Cache:
    def __init__(self) -> None:
        self._objects: dict[tuple[int, int, int], CachedObject] = {}
        self._groups: dict[int, GroupInfo] = {}
        self.downstream: Consumer | None = None

    def get_group(self, group_id: int) -> GroupInfo:
        return self._groups.setdefault(group_id, GroupInfo())

    def get_subgroup(self, group_id: int, sg_id: int) -> SubgroupInfo:
        group = self.get_group(group_id)
        return group.subgroups.setdefault(sg_id, SubgroupInfo())

    def put(self, obj: CachedObject) -> None:
        key = (obj.group_id, obj.subgroup_id, obj.object_id)
        self._objects[key] = obj
        group = self.get_group(obj.group_id)
        sg = group.subgroups.setdefault(obj.subgroup_id, SubgroupInfo())
        pos = bisect_left(sg.object_ids, obj.object_id)
        if pos >= len(sg.object_ids) or sg.object_ids[pos] != obj.object_id:
            sg.object_ids.insert(pos, obj.object_id)
            # Ensure subgroup is indexed at its next needed object
            if sg._contiguous_idx < len(sg.object_ids):
                group.add_pending(
                    obj.subgroup_id,
                    sg.object_ids[sg._contiguous_idx])

    def get(self, group_id: int, sg_id: int, obj_id: int) -> CachedObject:
        return self._objects[(group_id, sg_id, obj_id)]

    def has(self, group_id: int, sg_id: int, obj_id: int) -> bool:
        return (group_id, sg_id, obj_id) in self._objects

    def publish_object(self, group_id: int, subgroup_id: int,
                       object_id: int, payload: bytes) -> None:
        self.put(CachedObject(group_id, subgroup_id, object_id, payload))
        self._mark_known_range(self.get_group(group_id), object_id, object_id + 1)
        if self.downstream:
            self.downstream.publish_object(
                group_id, subgroup_id, object_id, payload)

    async def fetch_all_gaps(self, upstream: Upstream,
                             group_id: int) -> None:
        """Fetch all unknown intervals in the group from upstream."""
        group = self.get_group(group_id)
        for gap_start, gap_end in group.known_objects.gaps(
                0, group.total_objects):
            await upstream.fetch(group_id, gap_start, gap_end,
                                 _FetchWriter(self))
            self._mark_known_range(group, gap_start, gap_end)

    def _mark_known_range(self, group: GroupInfo, start: int, end: int) -> None:
        group.known_objects.add(start, end)
        # Only check subgroups whose next needed object is <= end
        for next_id in [k for k in group._pending_advance if k <= end]:
            for sg_id in group._pending_advance.pop(next_id):
                sg = group.subgroups[sg_id]
                was_contiguous = sg.integrity_from_start
                sg.try_advance(group.known_objects, resolved_end=end)
                # Re-index if still pending
                if sg._contiguous_idx < len(sg.object_ids):
                    group.add_pending(
                        sg_id, sg.object_ids[sg._contiguous_idx])
                if sg.integrity_from_start and not was_contiguous:
                    for observer in group.start_subgroup:
                        observer(sg_id)



class _FetchWriter:
    """Consumer that stores objects into cache without forwarding."""

    def __init__(self, cache: Cache) -> None:
        self._cache = cache

    def publish_object(self, group_id: int, subgroup_id: int,
                       object_id: int, payload: bytes) -> None:
        self._cache.put(CachedObject(group_id, subgroup_id, object_id, payload))


# ---------------------------------------------------------------------------
# FanOut — distributes to registered consumers
# ---------------------------------------------------------------------------

class FanOut:
    def __init__(self) -> None:
        self._consumers: list[Consumer] = []

    def add(self, consumer: Consumer) -> None:
        self._consumers.append(consumer)

    def remove(self, consumer: Consumer) -> None:
        self._consumers.remove(consumer)

    def publish_object(self, group_id: int, subgroup_id: int,
                       object_id: int, payload: bytes) -> None:
        for c in list(self._consumers):
            c.publish_object(group_id, subgroup_id, object_id, payload)


# ---------------------------------------------------------------------------
# Subscription — shared upstream subscribe task + fanout
# ---------------------------------------------------------------------------

class Subscription:
    def __init__(self, cache: Cache, upstream: Upstream) -> None:
        self.cache = cache
        self.upstream = upstream
        self.fanout = FanOut()
        cache.downstream = self.fanout
        self._task = asyncio.create_task(upstream.subscribe(cache))

    async def wait(self) -> None:
        await self._task


# ---------------------------------------------------------------------------
# SubscriberFilter — per-subscriber, per-subgroup FILLING/LIVE gating
# ---------------------------------------------------------------------------

class SubscriberFilter:
    def __init__(self, consumer: Consumer,
                 join_group: int,
                 fill_subgroups: set[int]) -> None:
        self.consumer = consumer
        self.join_group = join_group
        # Largest object_id delivered per subgroup in the join group.
        # None = fill in progress (suppress all live objects for this sg)
        # int  = fill complete up to here (suppress <= this id)
        # absent = fully live (pass through)
        self.largest_object: dict[int, int | None] = {
            sg_id: None for sg_id in fill_subgroups
        }

    def publish_object(self, group_id: int, subgroup_id: int,
                       object_id: int, payload: bytes) -> None:
        if group_id < self.join_group:
            return  # older group, already delivered or not needed
        if group_id == self.join_group:
            largest = self.largest_object.get(subgroup_id)
            if largest is None and subgroup_id in self.largest_object:
                return  # fill in progress
            if largest is not None and object_id <= largest:
                return  # already delivered during fill
        self.consumer.publish_object(
            group_id, subgroup_id, object_id, payload)

    def set_subgroup_live(self, subgroup_id: int,
                          last_object_id: int = -1) -> None:
        if last_object_id >= 0:
            self.largest_object[subgroup_id] = last_object_id
        else:
            self.largest_object.pop(subgroup_id, None)

    def add_fill_subgroup(self, subgroup_id: int) -> None:
        self.largest_object[subgroup_id] = None


# ---------------------------------------------------------------------------
# SendPacer — rate-limits emitted bytes
# ---------------------------------------------------------------------------

@dataclass(order=False)
class _PacerEntry:
    nbytes: int
    event: asyncio.Event


class SendPacer:
    """Rate-limits sends across concurrent emitters with priority ordering.

    Emitters register intent via ready_to_send(), then await their turn.
    The pacer wakes emitters in (group_id, subgroup_id) order.
    """

    def __init__(self, bytes_per_second: float) -> None:
        self._rate = bytes_per_second
        self._bytes_sent = 0
        self._start: float | None = None
        self._queue: list[tuple[int, int, _PacerEntry]] = []
        self._timer_pending = False

    def ready_to_send(self, nbytes: int, group_id: int,
                      sg_id: int) -> asyncio.Event:
        """Register intent to send. Returns an event to await."""
        entry = _PacerEntry(nbytes, asyncio.Event())
        heapq.heappush(self._queue, (group_id, sg_id, entry))
        self._wake_next()
        return entry.event

    def _wake_next(self) -> None:
        if not self._queue:
            return
        _, _, entry = self._queue[0]
        loop = asyncio.get_event_loop()
        now = loop.time()
        if self._start is None:
            self._start = now
        ready_at = self._start + self._bytes_sent / self._rate
        delay = ready_at - now
        if delay <= 0:
            heapq.heappop(self._queue)
            self._bytes_sent += entry.nbytes
            self._timer_pending = False
            entry.event.set()
            self._wake_next()
        elif not self._timer_pending:
            self._timer_pending = True
            loop.call_later(delay, self._wake_next)


# ---------------------------------------------------------------------------
# RelaySubscriptionHandler — orchestrates fill + live transition
# ---------------------------------------------------------------------------

class RelaySubscriptionHandler:
    def __init__(self, subscription: Subscription,
                 consumer: Consumer, largest_group: int,
                 num_fill_groups: int = 0,
                 send_rate: float = 0) -> None:
        self.subscription = subscription
        self.cache = subscription.cache
        self.consumer = consumer
        self.largest_group = largest_group
        self.num_fill_groups = num_fill_groups
        self.filter: SubscriberFilter | None = None
        self.pacer = SendPacer(send_rate) if send_rate > 0 else None

    async def run(self) -> None:
        first_fill = self.largest_group - self.num_fill_groups
        fill_subgroups: set[int] = set()
        group = self.cache.get_group(self.largest_group)
        for sg_id, sg in group.subgroups.items():
            if sg.integrity_from_start:
                fill_subgroups.add(sg_id)

        self.filter = SubscriberFilter(
            self.consumer, self.largest_group, fill_subgroups)
        self.subscription.fanout.add(self.filter)

        # Fill cached groups from first_fill through largest_group
        for group_id in range(first_fill, self.largest_group + 1):
            await self.fill_group(group_id)

        await self.subscription.wait()

    async def fill_group(self, group_id: int) -> None:
        group = self.cache.get_group(group_id)
        emitters: list[asyncio.Task] = []

        # Register observer — spawns an emitter when a subgroup
        # becomes contiguous (either now or during gap fetch)
        def start_subgroup(sg_id: int) -> None:
            if self.filter:
                self.filter.add_fill_subgroup(sg_id)
            emitters.append(asyncio.create_task(
                self.emit_subgroup(group_id, sg_id)))

        group.start_subgroup.append(start_subgroup)

        # Start emitters for already-contiguous subgroups
        for sg_id, sg_info in group.subgroups.items():
            if sg_info.integrity_from_start:
                start_subgroup(sg_id)

        # Gate the fetch — only one subscriber fetches per group.
        # Emitters for newly-discovered subgroups are spawned via
        # the start_subgroup observer during the fetch.
        if group.fetch_in_progress:
            await group.fetch_complete.wait()
        elif not group.fetch_complete.is_set():
            group.fetch_in_progress = True
            await self.cache.fetch_all_gaps(
                self.subscription.upstream, group_id)
            group.fetch_in_progress = False
            group.fetch_complete.set()

        group.start_subgroup.remove(start_subgroup)

        if emitters:
            await asyncio.gather(*emitters)

    async def emit_subgroup(self, group_id: int, sg_id: int) -> None:
        """Emit cached objects for a subgroup in order.

        Emits up to largest_contiguous without blocking. When it
        reaches the watermark, waits for the gap fetcher to advance
        it via contiguous_advanced.
        """
        sg = self.cache.get_subgroup(group_id, sg_id)
        pos = 0

        while pos < len(sg.object_ids):
            obj_id = sg.object_ids[pos]
            if obj_id > sg.largest_contiguous:
                # Wait for gap fetcher to advance the watermark
                sg.contiguous_advanced.clear()
                await sg.contiguous_advanced.wait()
                continue

            payload = self.cache.get(group_id, sg_id, obj_id).payload
            if self.pacer:
                event = self.pacer.ready_to_send(
                    len(payload), group_id, sg_id)
                await event.wait()
            self.consumer.publish_object(
                group_id, sg_id, obj_id, payload)
            pos += 1

        # Fill complete — transition to live
        last_obj = sg.object_ids[-1] if sg.object_ids else -1
        if self.filter:
            self.filter.set_subgroup_live(sg_id, last_obj)
