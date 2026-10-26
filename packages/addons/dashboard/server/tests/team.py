"""A small team for the settings tests: people added to the dev workspace with the store's own signed events."""

from orch import canon, crypto

from tests.identity.helpers import Person
from tests.store.helpers import T0, ts


def add_member(env, name: str, role: str) -> Person:
    person = Person()
    env.tick()
    env.store.append(
        env.person_event(
            env.owner,
            "workspace",
            "member.added",
            person=person.ref,
            name=name,
            role=role,
            pk_pub=crypto.b64u(person.pk_pub),
            device_cert=person.cert(created=T0 * 1000),
        ),
        log="workspace",
    )
    return person


def issue_grant(env, person: Person, hours: int = 4, scope: str = "all") -> str:
    from orch.identity import new_ulid

    env.tick()
    gid = "gr_" + new_ulid()
    issued = env.clock[0]
    env.store.append(
        env.person_event(
            person,
            "workspace",
            "grant.issued",
            grant=gid,
            scope=scope,
            verbs="agent",
            issued_at=ts(issued),
            hours=hours,
            expires_at=ts(issued + 3600 * hours),
            secret_hash=canon.grant_secret_hash(b"t" * 32),
            label="tests",
        ),
        log="workspace",
    )
    return gid
