"""
Offline smoke test for the whole simulation, with no Discord connection required.
Run with:  python test_offline.py
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

import config
config.DB_PATH = "/tmp/faction_test.db"
if os.path.exists(config.DB_PATH):
    os.remove(config.DB_PATH)

import db
from world import map_manager
from factions import manager as faction_manager
from military import manager as military_manager
from diplomacy import manager as diplomacy_manager
from economy import simulation
import leaderboards

GUILD_ID = 111111

USERS = {
    "alice": 1001, "bob": 1002, "carol": 1003, "dave": 1004, "erin": 1005, "frank": 1006,
}


async def main():
    await db.init_db()
    print("== Generating world ==")
    seed, w, h = await map_manager.generate_world(GUILD_ID, seed=42)
    print(f"seed={seed} size={w}x{h}")

    png = await map_manager.render_map_png(GUILD_ID)
    with open("/tmp/map_day0.png", "wb") as f:
        f.write(png)
    print("saved /tmp/map_day0.png")

    print("\n== Creating factions ==")
    f1 = await faction_manager.create_faction(GUILD_ID, USERS["alice"], "Redcliff", "#E74C3C")
    f2 = await faction_manager.create_faction(GUILD_ID, USERS["bob"], "Bluewater", "#3498DB")
    try:
        # should fail: too similar to Bluewater
        await faction_manager.create_faction(GUILD_ID, USERS["carol"], "Skytown", "#3498DC")
        print("!! ERROR: similar color was NOT rejected")
    except faction_manager.FactionError as e:
        print(f"OK color similarity rejected: {e}")
    f3 = await faction_manager.create_faction(GUILD_ID, USERS["carol"], "Skytown", "#27AE60")
    print(f"Created: {f1['name']}, {f2['name']}, {f3['name']}")

    # duplicate-faction-before-leaving check
    try:
        await faction_manager.create_faction(GUILD_ID, USERS["alice"], "Second", "#F1C40F")
        print("!! ERROR: allowed creating a 2nd faction without leaving first")
    except faction_manager.FactionError as e:
        print(f"OK second faction blocked: {e}")

    print("\n== Inviting decisioners ==")
    await faction_manager.invite_member(GUILD_ID, USERS["alice"], USERS["dave"])
    await faction_manager.invite_member(GUILD_ID, USERS["bob"], USERS["erin"])
    print("Dave -> Redcliff, Erin -> Bluewater")

    print("\n== Wanderer check ==")
    frank_member = await faction_manager.get_member(GUILD_ID, USERS["frank"])
    print(f"Frank is a wanderer at ({frank_member['wander_x']}, {frank_member['wander_y']})")
    try:
        moved = await faction_manager.move_wanderer(GUILD_ID, USERS["frank"], "east")
        print(f"Frank moved east to ({moved['x']},{moved['y']})")
    except faction_manager.FactionError as e:
        print(f"Frank could not move east: {e}")

    print("\n== Inhabitant transfer ==")
    request = await faction_manager.create_inhabitant_request(
        GUILD_ID, USERS["alice"], "Bluewater", 5, food_offer=20, treasury_offer=10
    )
    transfer = await faction_manager.resolve_inhabitant_request(
        GUILD_ID, USERS["bob"], request["request_id"], True
    )
    print(
        f"Transferred {transfer['request']['inhabitants']} workers from "
        f"{transfer['donor']['name']} to {transfer['requester']['name']}"
    )

    print("\n== Running 5 economic ticks ==")
    for _ in range(5):
        summary = await simulation.run_tick(GUILD_ID)
        disaster_note = ""
        if summary["disaster"]:
            disaster_note = f" | DISASTER: {summary['disaster']['label']} ({summary['disaster']['casualties']} casualties)"
        print(f"Day {summary['day']}:" + disaster_note)
        for fid, d in summary["factions"].items():
            print(f"   {d['name']}: food+{d['food_prod']:.1f} money+{d['money_prod']:.1f} "
                  f"deaths={d['deaths']} migration={d['migration']}")

    print("\n== Military: convert and conquer ==")
    faction1 = await faction_manager.get_faction_by_id(f1["faction_id"])
    print(f"Redcliff workers before: {faction1['worker_count']}")
    await military_manager.convert_to_militia(GUILD_ID, USERS["alice"], 10)
    faction1 = await faction_manager.get_faction_by_id(f1["faction_id"])
    print(f"Redcliff militia after conversion: {faction1['militia_count']}")

    candidates = await map_manager.get_adjacent_unclaimed_or_enemy(
        GUILD_ID, f1["faction_id"]
    )
    if candidates:
        target = candidates[0]
        conquer_result = await military_manager.conquer(
            GUILD_ID, USERS["alice"], target["x"], target["y"], 3
        )
        print(
            f"Conquer attempt on ({target['x']},{target['y']}): "
            f"success={conquer_result['success']} "
            f"chance={conquer_result['chance']:.2f} "
            f"militia_lost={conquer_result['militia_lost']}"
        )

    print("\n== Diplomacy: alliance ==")
    await diplomacy_manager.propose(GUILD_ID, USERS["alice"], "Bluewater", "alliance")
    await diplomacy_manager.accept(GUILD_ID, USERS["bob"], "Redcliff", "alliance")
    relations = await diplomacy_manager.list_relations(GUILD_ID, f1["faction_id"])
    print(f"Redcliff relations: {[(r['kind'], r['status']) for r in relations]}")

    print("\n== Attack should be blocked by alliance ==")
    try:
        await military_manager.attack_faction(GUILD_ID, USERS["alice"], "Bluewater", 1)
        print("!! ERROR: attack was not blocked by alliance")
    except military_manager.MilitaryError as e:
        print(f"OK attack blocked: {e}")

    print("\n== Attack an unallied faction ==")
    await military_manager.convert_to_militia(GUILD_ID, USERS["carol"], 5)  # give Skytown something moot
    try:
        atk = await military_manager.attack_faction(GUILD_ID, USERS["bob"], "Skytown", 3)
        print(f"Bluewater attacks Skytown -> attacker_wins={atk['attacker_wins']}")
    except military_manager.MilitaryError as e:
        print(f"Attack raised: {e}")

    print("\n== Leadership transfer (handoff within faction) ==")
    result = await faction_manager.transfer_leadership(GUILD_ID, USERS["alice"], USERS["dave"])
    print(f"Transfer type: {result['type']}, new leader should be Dave")
    f1_after = await faction_manager.get_faction_by_id(f1["faction_id"])
    print(f"Redcliff leader_id now: {f1_after['leader_id']} (expected {USERS['dave']})")

    print("\n== Leadership merge (faction incorporation) ==")
    # Dave (Redcliff leader) gives control to Bob (Bluewater leader) -> Redcliff merges into Bluewater
    merge_result = await faction_manager.transfer_leadership(GUILD_ID, USERS["dave"], USERS["bob"])
    print(f"Merge type: {merge_result['type']}")
    bluewater_after = await faction_manager.get_faction_by_id(f2["faction_id"])
    land = await map_manager.count_faction_land(GUILD_ID, f2["faction_id"])
    print(f"Bluewater now has {bluewater_after['worker_count']} workers and {land} tiles after merge")

    print("\n== Leaderboards ==")
    for metric in ("food", "money", "inhabitants", "land"):
        entries = await leaderboards.build_leaderboard(metric, guild_id=GUILD_ID)
        print(f"{metric}: " + ", ".join(f"{e['faction']['name']}={e['value']}" for e in entries))

    print("\n== Final map render ==")
    png = await map_manager.render_map_png(GUILD_ID)
    with open("/tmp/map_final.png", "wb") as f:
        f.write(png)
    print("saved /tmp/map_final.png")

    print("\nALL OFFLINE TESTS COMPLETED.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    finally:
        asyncio.run(db.close_db())
