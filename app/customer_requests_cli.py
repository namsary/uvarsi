#!/usr/bin/env python3
"""Owner-only CLI for reviewing withdrawal and complaint requests."""

import argparse
import datetime
import json
import os
from contextlib import closing

import customer_requests
import db_rezim
import naklady
import platby


DB = os.environ.get("UVARSI_DB", "/opt/uvarsi/uvarsi.db")


def _open():
    con = db_rezim.otvor(DB)
    customer_requests.migrate_customer_requests_schema(con)
    platby.migrate_platby_schema(con)
    con.commit()
    return con


def _list(con) -> int:
    rows = con.execute(
        """SELECT public_id,order_id,request_type,status,refund_scope,
                  created_at,updated_at
             FROM consumer_requests
            ORDER BY CASE WHEN status IN ('received','processing','requires_review')
                          THEN 0 ELSE 1 END, created_at"""
    ).fetchall()
    for row in rows:
        print(json.dumps(dict(row), ensure_ascii=False, separators=(",", ":")))
    print(f"Spolu nevyriešených: {customer_requests.count_unresolved_requests(con)}")
    return 0


def _processing(con, public_id: str) -> int:
    if not customer_requests.mark_processing(
        con, public_id=public_id, now=datetime.datetime.now().timestamp()
    ):
        print("Žiadosť sa nenašla alebo už nie je v stave na spracovanie.")
        return 1
    print("Žiadosť je označená ako spracúvaná.")
    return 0


def _notify(con) -> int:
    count = customer_requests.count_unresolved_requests(con)
    if count <= 0:
        print("Nevyriešené žiadosti: 0")
        return 0
    now = datetime.datetime.now(datetime.timezone.utc)
    key = f"consumer_requests:{now.date().isoformat()}:{count}"
    if platby.zaznamenaj_upozornenie(con, kluc=key, now=now):
        naklady.posli_ntfy({
            "titul": "Uvar.si: čakajú požiadavky zákazníkov",
            "sprava": (
                f"Nevyriešené odstúpenia a reklamácie: {count}. "
                "Otvor chránenú evidenciu požiadaviek."
            ),
        })
    print(f"Nevyriešené žiadosti: {count}")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="customer_requests_cli.py")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list", help="vypíše žiadosti v chránenej konzole")
    processing = commands.add_parser(
        "processing", help="označí žiadosť ako spracúvanú"
    )
    processing.add_argument("public_id")
    commands.add_parser("notify", help="pošle iba anonymný denný súhrn")
    args = parser.parse_args(argv)
    with closing(_open()) as con:
        if args.command == "list":
            return _list(con)
        if args.command == "processing":
            return _processing(con, args.public_id)
        return _notify(con)


if __name__ == "__main__":
    raise SystemExit(main())
