"""Materialize the reviewed bundled sample release in a funded tenant cache."""
import argparse
import json
from backend import curated_cloud, db


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--user',required=True)
    parser.add_argument('--actor',required=True)
    args=parser.parse_args()
    actor=db.query('SELECT id,role,frozen FROM users WHERE id=?',(args.actor,),one=True)
    if not actor:raise ValueError('Administrator not found')
    print(json.dumps(curated_cloud.publish(actor,args.user)))


if __name__=='__main__':
    try:main()
    finally:
        if db.DATABASE_URL:
            from backend.postgres import close
            close()
