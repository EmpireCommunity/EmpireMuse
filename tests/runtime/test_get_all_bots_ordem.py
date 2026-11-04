"""``get_all_bots`` segue a ordem do ``TOKEN`` (o principal primeiro) e não repete bots."""

from types import SimpleNamespace

from empiremuse.utils.client import BotPool


def test_get_all_bots_keeps_token_order_and_removes_duplicates():
    main, r1, r2, r3, exclusive = (object() for _ in range(5))  # hashable, como os bots de verdade
    pool = SimpleNamespace(bots=[main, r1, r2, r3], guild_bots={"1": [exclusive, r2]})

    result = BotPool.get_all_bots(pool)

    assert result == [main, r1, r2, r3, exclusive]
