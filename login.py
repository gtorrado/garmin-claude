"""One-time Garmin login (handles 2FA). Your password is never stored.

    python login.py                 # prints a token to paste into GARMIN_TOKENS
    python login.py --save PATH     # writes the token file directly (used by setup-vps.sh)
"""

import getpass
import sys

from garminconnect import Garmin

save_to = sys.argv[sys.argv.index("--save") + 1] if "--save" in sys.argv else None

email = input("Garmin email: ").strip()
password = getpass.getpass("Garmin password (typing is hidden): ")

garmin = Garmin(email=email, password=password,
                prompt_mfa=lambda: input("2FA code from Garmin (email/app): ").strip())
garmin.login()
print(f"\nLogged in to Garmin as {garmin.get_full_name()}.")

if save_to:
    garmin.client.dump(save_to)
    print("Login saved on the server.")
else:
    print("Copy EVERYTHING on the next line into the GARMIN_TOKENS secret:\n")
    print(garmin.client.dumps())
    print("\nKeep it private: it gives access to your Garmin account "
          "(revoke by changing your Garmin password).")
