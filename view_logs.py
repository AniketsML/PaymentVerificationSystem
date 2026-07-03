"""
Debug helper: show the COMPLETE journey of any lead by its id.

    python view_logs.py LEAD-42
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from observability.pg_logger import PgLeadLogger


def main():
    if len(sys.argv) < 2:
        print("usage: python view_logs.py <lead_id>")
        return
    lead_id = sys.argv[1]
    journey = PgLeadLogger().get_lead_journey(lead_id)

    print(f"\n=== LEAD {lead_id} ===")
    final = journey["final"]
    if final:
        print(f"FINAL: {final['verification_status']}   payment_method={final['payment_method']}")
        print(f"OUTCOME: {final['outcome']}")
    print("\nJOURNEY:")
    for e in journey["journey"]:
        mark = "OK " if e["status"] == "PASS" else "XX "
        ms = f" ({e['ms']:.0f} ms)" if e.get("ms") else ""
        print(f"  {mark}[{e['stage']}]{ms} {e['reason']}")
        if e["metrics"]:
            print(f"       metrics: {json.dumps(e['metrics'], ensure_ascii=False)}")
        raw = (e.get("data") or {}).get("raw_model_response")
        if raw:
            print(f"       raw model response: {raw[:600]}")
    if not journey["journey"]:
        print("  (no events found for this lead id)")


if __name__ == "__main__":
    main()
