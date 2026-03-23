---
title: "Join Subscription Filters for Media over QUIC Transport"
abbrev: "moqt-join"
category: info

docname: draft-frindell-moq-join-filters-latest
submissiontype: IETF  # also: "independent", "editorial", "IAB", or "IRTF"
number:
date:
consensus: true
v: 3
area: "Web and Internet Transport"
workgroup: "Media Over QUIC"
keyword:
 - next generation
 - unicorn
 - sparkling distributed ledger
venue:
  group: "Media Over QUIC"
  type: "Working Group"
  mail: "moq@ietf.org"
  arch: "https://mailarchive.ietf.org/arch/browse/moq/"
  github: "afrind/draft-frindell-moq-join-filters"
  latest: "https://afrind.github.io/draft-frindell-moq-join-filters/draft-frindell-moq-join-filters.html"

author:
 -
    fullname: "afrind"
    organization: Your Organization Here
    email: "afrind@users.noreply.github.com"

normative:

informative:

...

--- abstract

This document defines two new MOQT Subscription Filters -- Join
Relative Group and Join Absolute Group -- that enable a subscriber
to receive both historical and live content on a single
subscription.  Unlike Joining FETCH, which delivers past objects on
a single stream with head-of-line blocking, Join filters deliver
historical content using SUBSCRIBE semantics: per-subgroup streams,
subject to delivery timeout, priorities, and datagram forwarding
preferences.  The publisher uses a combination of cache and upstream
FETCH to assemble complete groups for delivery.


--- middle

# Introduction

When a subscriber joins a track mid-stream, it often needs recent
historical content to begin playback.  MOQT provides two mechanisms
today:

- SUBSCRIBE delivers future content using per-subgroup streams,
  allowing independent progress and partial delivery.

- FETCH / Joining FETCH delivers past content on a single stream,
  guaranteeing completeness but introducing head-of-line blocking
  across the entire range.

For latency-sensitive applications, the single-stream delivery of
Joining FETCH is problematic.  Objects from earlier groups block
delivery of later, more relevant objects.  A subscriber that joins
during group 10 and requests groups 5-10 must wait for group 5 to
fully transfer before receiving group 6, even if group 6 is
immediately available.

This document defines Join filters, which instruct the publisher to
deliver historical groups using SUBSCRIBE semantics.  Each subgroup
is delivered on its own stream (or via datagrams), subject to the
subscription's delivery timeout, group order, and priorities.  The
publisher assembles complete groups from its cache, supplemented by
upstream FETCHes for any missing objects.


# Conventions and Definitions

{::boilerplate bcp14-tagged}


# Subscription Filters

## Join Relative Group

~~~
Subscription Filter {
  Filter Type (vi64) = 0xTBD,
  Num Fill Groups (vi64),
}
~~~

The subscriber requests delivery starting from Num Fill Groups
groups before the current group.  A value of 0 means delivery
starts at the current (join) group.  A value of 2 means the join
group plus the 2 most recent complete groups.

The publisher resolves the actual starting group based on the live
edge at subscription time.  The join group -- the group containing
the Largest Location reported in SUBSCRIBE_OK -- receives
per-subgroup fill/live treatment ({{join-group-transition}}).  Groups before the
join group are delivered entirely via fill, even if objects in
those groups arrive after the subscription is established.

## Join Absolute Group

~~~
Subscription Filter {
  Filter Type (vi64) = 0xTBD,
  Start Group (vi64),
}
~~~

The subscriber requests delivery starting from the specified group.
The publisher determines the join group as with Join Relative Group.


# SUBSCRIBE_OK

The SUBSCRIBE_OK for a Join filter includes:

- Largest Location: the live edge at subscription time, defining the
  join group.

- FILL_START message parameter (0xTBD): the group ID of the first
  group being filled.  This may differ from what the subscriber
  requested based on publisher policy.

The subscriber can compare FILL_START with its request to determine
if the publisher honored the full range.

If FILL_START is absent, the publisher has converted the
subscription to a NextGroup filter.  Merging historical and live
content requires the publisher to buffer at least the current
group; a publisher that cannot or will not do so MUST omit
FILL_START.  The subscriber MAY issue standalone FETCHes to
retrieve historical content.

It's possible that a requested group is not in cache and no longer
retrievable via upstream FETCH.  In this case, the subscriber does
not get any signal that the old groups will not be delivered.


# Fill Semantics

Groups in the fill range (FILL_START through the join group) are
delivered using SUBSCRIBE semantics.  The publisher MUST attempt
to deliver each subgroup and datagram that exists in each fill
group -- relays assemble complete groups from cache and upstream
FETCH as needed.

## Per-Group Fill

For each group in the fill range, the publisher:

1. Identifies cached objects and which object ID ranges are known
   (present or confirmed absent).

2. Relay publishers issue upstream FETCHes for any unknown ranges
   within the group.

3. Delivers objects per-subgroup as they become available.

The publisher is not required to deliver initiate delivery of fill
groups in order -- a relay MAY begin delivering cached content while
waiting for upstream FETCHes to complete. However, within each
subgroup, objects MUST be delivered in object ID order.

## Subgroup Integrity

A publisher MUST NOT begin delivering a subgroup stream for a fill
group until it can confirm integrity from the start -- that is, it
has resolved (via cache or fetch) all object IDs from 0 through at
least the first object in the subgroup.  Otherwise it cannot know
that it has the first object in the subgroup.

As gaps are resolved, additional subgroups can become deliverable.
The publisher MAY begin delivering these subgroups as soon as their
integrity is established, without waiting for other subgroups or
gaps to complete.

## Join Group Transition {#join-group-transition}

The join group receives special treatment.  New content arrives via
the live subscription concurrently with fill delivery from cache.
Every subgroup in the join group begins in fill mode and transitions
to live after fill completes.

For each subgroup, the publisher tracks the largest object ID
received (from cache or live) at the time fill began.  The
publisher delivers cached objects for that subgroup in order.  Once
it has delivered up to and including the largest received object,
the subgroup transitions to live: subsequent objects from the
upstream subscription are forwarded directly.  Objects received
via the live subscription while fill is in progress are suppressed
to avoid duplicates.

Subgroups not yet known at subscription time -- including those
discovered during gap fetching -- also begin in fill mode.  A
subgroup stream cannot be initiated until the publisher can confirm
it has the correct start (see {{subgroup-integrity}}).  Once a newly
discovered subgroup establishes integrity, it is filled from cache
and transitions to live as above.

Each subgroup transitions independently.  A subgroup that completes
fill early begins receiving live content immediately, while other
subgroups may still be filling.

## Groups Before the Join Group

Groups before the join group are delivered entirely from fill.  Live
content for these groups (arriving via the upstream subscription) is
not forwarded to the subscriber.

## Groups After the Join Group

Groups after the join group are delivered entirely from the live
stream, using normal SUBSCRIBE semantics.


# Delivery Constraints

Fill content is subject to the same delivery constraints as live
content:

- Delivery Timeout: Groups or objects that exceed the delivery
  timeout MAY be dropped, as with any SUBSCRIBE delivery.  The
  delivery timeout starts when an object is read from cache and
  queued to send.

- Group Order: Fill groups are delivered respecting the
  subscription's group order.  With "descending" order, the join
  group is prioritized over older groups.

- Subgroup Priorities: Within a group, subgroups are delivered
  according to their publisher-assigned priorities.

- Send Rate: If the subscription specifies a Send Rate parameter,
  fill delivery is paced accordingly.  Higher-priority subgroups
  are served first when multiple subgroups compete for send
  capacity.  Live data is not subject to the Send Rate limit.

- Datagrams: Objects with datagram forwarding preference are
  delivered via datagrams, as with normal SUBSCRIBE delivery.


# Relay Behavior

## Upstream Subscription

A relay receiving a Join filter SHOULD consider its cache state
before subscribing upstream.  If the relay already has complete
groups in cache, it SHOULD NOT request those groups again via an
upstream Join filter, as this would result in redundant delivery of
already-cached objects.  Instead, the relay SHOULD subscribe
upstream with a filter that covers only the groups it does not
already have, or use a standard Latest Group / Latest Object filter
and issue targeted FETCHes for any missing ranges.

If the upstream publisher does not support Join filters, the relay
MAY fall back to a combination of SUBSCRIBE with a standard filter
and upstream FETCHes to populate its cache, then serve the
downstream subscriber using Join semantics from its own cache.

## Shared State

Multiple downstream subscribers may request Join filters for the
same track with different fill ranges.  Each subscriber maintains
independent fill/live transition state per subgroup.

## Datagram Handling

Objects with datagram forwarding preference do not use subgroup
streams and therefore cannot transition from fill to live in the
same way.  For datagrams in the join group, the publisher uses the
object ID of the Largest Location at subscription time as the
boundary: objects with ID less than or equal to the largest are
delivered via fill (from cache), and objects with ID greater than
the largest are delivered via live (from the upstream subscription).
The publisher suppresses live datagrams that fall within the fill
range to avoid duplicates.

## Upstream FETCH

When the relay's cache has gaps in a fill group, it issues upstream
FETCHes to retrieve the missing ranges.  The relay SHOULD gate
fetches so that only one fetch per gap range is in flight, sharing
results across subscribers that need the same group.


# Re-join and Publish Join

## Re-join via REQUEST_UPDATE

A subscriber MAY send a REQUEST_UPDATE with a Join Relative Group
or Join Absolute Group filter to re-join a track after a gap in
delivery -- for example, after a subscription transitions from
forward=0 back to forward=1.

Upon receiving a REQUEST_UPDATE with a Join filter, the publisher:

1. Resets any open subgroup streams for the current group.
2. Treats the request as a fresh join: determines a new join group
   at the current live edge, computes the fill range from the
   filter, and begins fill delivery.
3. Sends REQUEST_OK with the new Largest Location and FILL_START
   parameter.

The subscriber MAY receive duplicate objects for the group that
was in progress when the original subscription paused, since
subgroup streams are reset and fill restarts from the beginning
of that group.  This avoids the need to track per-subgroup
delivery state across the gap.

## Publish Join

When a relay begins publishing a track (e.g., taking over from a
previous publisher or joining a publish mid-stream), it may have
cached content from the previous publisher but no active upstream
subscription.

The relay MAY include the FILL_START parameter in PUBLISH_OK to
indicate that it will fill subscribers from the specified group
using Join semantics.  This allows the relay to serve cached
content to existing subscribers using the same fill algorithm as
a Join filter subscription, without requiring subscribers to
re-subscribe.


# Interaction with Joining FETCH

A Join filter subscription is intended to replace the need for a
Joining FETCH, since historical content is delivered with SUBSCRIBE
semantics directly.

However, if the publisher omits FILL_START in SUBSCRIBE_OK
(converting the subscription to NextGroup), the subscriber MAY
issue standalone FETCHes to retrieve historical content.


# Security Considerations

TODO Security


# IANA Considerations

Register two new Subscription Filter types and one new Message
Parameter:

- Filter Type 0xTBD: Join Relative Group
- Filter Type 0xTBD: Join Absolute Group
- Message Parameter 0xTBD: FILL_START (in SUBSCRIBE_OK, REQUEST_OK,
  and PUBLISH_OK)


--- back

# Acknowledgments
{:numbered="false"}

TODO acknowledge.
