"""Sorted non-overlapping interval set."""


class IntervalSet:
    """Maintains a sorted list of non-overlapping [start, end) intervals."""

    def __init__(self) -> None:
        self._intervals: list[tuple[int, int]] = []

    def add(self, start: int, end: int) -> None:
        """Add interval [start, end). Merges with overlapping/adjacent."""
        if start >= end:
            return
        new: list[tuple[int, int]] = []
        s, e = start, end
        inserted = False
        for lo, hi in self._intervals:
            if hi < s:
                new.append((lo, hi))
            elif lo > e:
                if not inserted:
                    new.append((s, e))
                    inserted = True
                new.append((lo, hi))
            else:
                s = min(s, lo)
                e = max(e, hi)
        if not inserted:
            new.append((s, e))
        self._intervals = new

    def contains(self, val: int) -> bool:
        for lo, hi in self._intervals:
            if lo <= val < hi:
                return True
            if lo > val:
                break
        return False

    def covers_range(self, start: int, end: int) -> bool:
        """Return True if every integer in [start, end) is covered."""
        if start >= end:
            return True
        for lo, hi in self._intervals:
            if lo <= start and hi >= end:
                return True
            if lo > start:
                break
        return False

    def gaps(self, start: int, end: int) -> list[tuple[int, int]]:
        """Return uncovered sub-intervals within [start, end)."""
        result: list[tuple[int, int]] = []
        cursor = start
        for lo, hi in self._intervals:
            if lo >= end:
                break
            if hi <= cursor:
                continue
            gap_start = cursor
            gap_end = min(lo, end)
            if gap_start < gap_end:
                result.append((gap_start, gap_end))
            cursor = max(cursor, hi)
        if cursor < end:
            result.append((cursor, end))
        return result

    def __repr__(self) -> str:
        return f"IntervalSet({self._intervals})"
