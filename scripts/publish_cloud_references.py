"""Run on the application host; dry-run unless --execute is supplied."""
import argparse
import json
from pathlib import Path

from backend import cloud_references, db


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--user', required=True, help='Destination funded workspace ID')
    parser.add_argument('--actor', required=True, help='Authorizing administrator ID')
    parser.add_argument('--acquisition', default='evidence/deployment/reference-acquisition.json',
                        help='Completed exact legacy receipt or schema-2 reviewed bundle receipt (never a remote URL)')
    parser.add_argument('--execute', action='store_true')
    args = parser.parse_args()
    actor = db.query('SELECT id,role,frozen FROM users WHERE id=?', (args.actor,), one=True)
    if not actor:
        raise ValueError('Administrator was not found')
    acquisition = json.loads(Path(args.acquisition).read_text())
    result = cloud_references.publish(actor, args.user, acquisition, execute=args.execute)
    # Receipt contains identities and state only, never signed URLs or secrets.
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    try:
        main()
    finally:
        if db.DATABASE_URL:
            from backend.postgres import close
            close()
