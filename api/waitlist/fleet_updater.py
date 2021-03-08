import threading
import time
import logging
from typing import Any, Dict, Set
import sqlalchemy
from .data import messager, esi
from .data.database import Session, Fleet, FleetSquad, WaitlistEntry, WaitlistEntryFit


LOG = logging.getLogger(__name__)


def notify_waitlist_update(waitlist_id: int) -> None:
    messager.MESSAGER.send_json(
        ["waitlist"], "waitlist_update", {"waitlist_id": waitlist_id}
    )


def update_fleet(session: sqlalchemy.orm.session.Session, fleet: Fleet) -> None:
    # Lock the fleet by starting a transaction
    fleet.is_updating = True

    try:
        members_raw = esi.get("/v1/fleets/%d/members" % fleet.id, fleet.boss_id).json()
    except (esi.HTTP404, esi.HTTP403):
        # Fleet no longer exists
        session.query(FleetSquad).filter(FleetSquad.fleet_id == fleet.id).delete()
        session.delete(fleet)  # type: ignore
        session.commit()
        return

    members = {member["character_id"]: member for member in members_raw}

    waitlist_ids = _update_waitlist(session, members)

    # (almost) All done!
    fleet.is_updating = False
    session.commit()

    # Do this after releasing the lock, in case the notifications take a while
    for waitlist_id in waitlist_ids:
        notify_waitlist_update(waitlist_id)


def _update_waitlist(
    session: sqlalchemy.orm.session.Session, members: Dict[int, Dict[str, Any]]
) -> Set[int]:
    waitlist_ids = set()
    member_ids = set(members.keys())
    for entry_fit in session.query(WaitlistEntryFit):
        if entry_fit.character_id in member_ids:
            session.query(WaitlistEntryFit).filter(
                WaitlistEntryFit.entry_id == entry_fit.entry_id
            ).delete()
            entry = (
                session.query(WaitlistEntry)
                .filter(WaitlistEntry.id == entry_fit.entry_id)
                .one_or_none()
            )
            if entry:
                session.delete(entry)  # type: ignore
                waitlist_ids.add(entry.waitlist_id)

    return waitlist_ids


def fleet_updater() -> None:
    while True:
        session = Session()
        sleep_time = 6  # ESI docs say fleets are cached for "up to 5 seconds"
        try:
            for fleet in session.query(Fleet).all():
                update_fleet(session, fleet)
        except Exception:  # pylint: disable=broad-except
            session.rollback()
            LOG.exception("Failed to run fleet updater")
            sleep_time = 30
        finally:
            session.close()

        time.sleep(sleep_time)


def create_fleet_updater() -> threading.Thread:
    return threading.Thread(target=fleet_updater, daemon=True)
