"""#25: the string `post_id` every telemetry client depends on is a contract.

`FeedEvent.post_id` is `Optional[int]`, and a browser MUST send it as a STRING:
real ids are HAF block/op-derived and exceed JavaScript's safe-integer range, so
`Number()`-ing one corrupts it silently.

    id           : 469505934805172229
    2^53         :      9007199254740992
    as JS Number : 469505934805172224   <- 5 off, no error

The feed already serves it as text for that reason (`::text` at three sites in
endpoints/endpoint_schema.sql, per !32), Waggle passes it through untouched
(`telemetry.ts`, "never Number()"), and PeakD has been asked to do the same.

It works only because pydantic v2's LAX mode coerces a numeric string to int. That
is an implicit property of a default, not an asserted contract: turning on strict
mode, adding `model_config = ConfigDict(strict=True)`, or a pydantic v3 default
change would start 422ing every integrator's entire batch -- one bad event rejects
the whole request -- while the suite stayed green, because every tavern case sends
a small bare integer (123, 55, 1) and no test referenced FeedEvent at all.
"""

import pytest
from pydantic import ValidationError

from haf_fyp.api import EventsBatch, FeedEvent

# A real id from the shipped feed. Above 2^53, so it is exactly the value a client
# cannot send as a JSON number without corrupting it.
BIG_ID = 469505934805172229
BIG_ID_STR = "469505934805172229"


class TestPostIdArrivesAsAString:
    """The contract integrators actually build against."""

    def test_a_string_id_is_accepted(self):
        assert FeedEvent(t="impression", post_id=BIG_ID_STR).post_id == BIG_ID

    def test_it_is_coerced_losslessly_above_2_53(self):
        # The whole point: no float round-trip anywhere in the path.
        got = FeedEvent(t="impression", post_id=BIG_ID_STR).post_id
        assert got == BIG_ID, f"{got} != {BIG_ID} (differs by {got - BIG_ID})"
        assert isinstance(got, int)
        assert str(got) == BIG_ID_STR

    def test_a_bare_integer_still_works(self):
        # Server-side and test callers send ints; both shapes must keep working.
        assert FeedEvent(t="impression", post_id=BIG_ID).post_id == BIG_ID

    def test_a_whole_batch_of_string_ids_validates(self):
        batch = EventsBatch(
            session="s-1",
            events=[
                {"t": "impression", "post_id": BIG_ID_STR, "rank": 0},
                {"t": "action", "post_id": BIG_ID_STR, "kind": "vote"},
            ],
        )
        assert [e.post_id for e in batch.events] == [BIG_ID, BIG_ID]

    def test_the_model_is_not_in_strict_mode(self):
        """The single assertion that would fail the day someone turns it on.

        Named so the failure explains itself: strict mode is a reasonable-looking
        change that would break every integrator at once, and the tests above would
        then all fail with type errors that read like test bugs rather than like a
        deliberate contract being broken.
        """
        assert FeedEvent.model_config.get("strict") is not True, (
            "FeedEvent is strict: clients send post_id as a STRING because real ids "
            "exceed 2^53, and strict mode 422s their entire batch. If this is "
            "intended, change post_id's type and tell integrators first."
        )


class TestWhatIsStillRejected:
    """Accepting strings must not mean accepting anything."""

    @pytest.mark.parametrize("bad", ["not-a-number", "", "12x", "0x1f", "1.5"])
    def test_non_integer_strings_are_refused(self, bad):
        with pytest.raises(ValidationError):
            FeedEvent(t="impression", post_id=bad)

    @pytest.mark.parametrize("bad", [0, -1, "0", "-5"])
    def test_non_positive_ids_are_refused(self, bad):
        # gt=0 on the field; a client sending 0 for "no post" should hear about it
        # rather than have it written to feed_events.
        with pytest.raises(ValidationError):
            FeedEvent(t="impression", post_id=bad)

    def test_post_id_stays_optional(self):
        # Impressions of the feed itself carry no post.
        assert FeedEvent(t="impression").post_id is None


class TestTheColumnCanHoldIt:
    """post_id lands in a bigint column; the contract only holds while it fits."""

    def test_the_id_fits_in_a_postgres_bigint(self):
        assert BIG_ID < 2**63 - 1
