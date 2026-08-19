"""Generate the bot's trading wallet INSIDE the droplet's .env -- the key
is born where it works and never travels.

Prints ONLY the public address. Refuses to touch an existing key. The
private key controls real funds: it must never appear in chat, logs,
shell history, or the repo (.env is gitignored; this script never prints
it and nothing else reads it except bot/live.py via os.environ).

    python3 src/make_wallet.py
"""
import os
import sys


def main():
    path = ".env"
    if os.path.exists(path):
        for line in open(path):
            if line.strip().startswith("POLYMARKET_PRIVATE_KEY="):
                print("a POLYMARKET_PRIVATE_KEY already exists in .env -- "
                      "refusing to overwrite. Remove that line first if you "
                      "really mean to rotate the wallet (and move any funds "
                      "off the old address before you do).")
                sys.exit(1)
    try:
        from eth_account import Account
    except ImportError:
        print("eth_account missing -- run: pip3 install "
              "'polymarket-client>=0.6' --break-system-packages")
        sys.exit(1)
    acct = Account.create()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a") as fh:
        fh.write(f"POLYMARKET_PRIVATE_KEY={acct.key.hex()}\n")
    os.chmod(path, 0o600)
    print(f"bot wallet address: {acct.address}")
    print("key written to .env (mode 600). Fund this address with USDC on "
          "POLYGON (website withdraw works), then run "
          "src/probe_live.py.")


if __name__ == "__main__":
    main()
