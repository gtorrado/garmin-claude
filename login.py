"""Run this ONCE on your own computer to log in to Garmin (handles 2FA).

It prints a token string. Paste it into your host's GARMIN_TOKENS secret.
Your password is never stored anywhere.

    pip install garminconnect==0.3.20
    python login.py
"""

import getpass

from garminconnect import Garmin

email = input("Garmin email: ").strip()
password = getpass.getpass("Garmin password: ")

garmin = Garmin(email=email, password=password,
                prompt_mfa=lambda: input("2FA code from Garmin (email/app): ").strip())
garmin.login()
print(f"\nLogged in as {garmin.get_full_name()}.\n")
print("Copy EVERYTHING on the next line into the GARMIN_TOKENS secret:\n")
print(garmin.client.dumps())
print("\nKeep it private: it gives access to your Garmin account (revoke by changing your password).")
