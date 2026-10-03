"""Run only after the owner authorizes transferring these credentials."""
import argparse
import os
import secrets
import subprocess
from pathlib import Path

def run(command, payload):
    result = subprocess.run(command, input=payload, capture_output=True)
    if result.returncode:
        # CLI diagnostics may contain secret values; intentionally omit them.
        raise RuntimeError('Secret configuration failed: ' + command[0])

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--firebase-file', required=True)
    parser.add_argument('--account-id', required=True)
    parser.add_argument('--service-url', required=True)
    args = parser.parse_args()
    os.environ['CLOUDFLARE_ACCOUNT_ID'] = args.account_id
    account = Path(args.firebase_file).read_bytes()
    token = secrets.token_urlsafe(48).encode()
    repo = 'EPAV-GAME/epav-swift-images'
    run(['node','worker/node_modules/wrangler/bin/wrangler.js','secret','put','IMAGE_SYNC_TOKEN','--config','worker/wrangler.jsonc','--profile','epav'], token)
    run(['gh','secret','set','FIREBASE_SERVICE_ACCOUNT_JSON','--repo',repo],account)
    run(['gh','secret','set','IMAGE_SYNC_TOKEN','--repo',repo],token)
    run(['gh','variable','set','IMAGE_SERVICE_URL','--repo',repo],args.service_url.encode())
    run(['gh','variable','set','SYNC_ENABLED','--repo',repo],b'true')
    print('Cloudflare and GitHub secrets configured; daily synchronization enabled.')

if __name__ == '__main__': main()
