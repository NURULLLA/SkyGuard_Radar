from FlightRadar24 import FlightRadar24API
import logging

logging.basicConfig(level=logging.INFO)
fr_api = FlightRadar24API()

regs = ["UK75057", "UK75058", "UK-75057", "UK-75058"]

print("--- Testing get_flights() ---")
all_f = fr_api.get_flights()
print(f"Total flights found: {len(all_f)}")

for f in all_f:
    if f.registration and f.registration.replace("-", "").upper() in [r.replace("-", "").upper() for r in regs]:
        print(f"FOUND in all_f: {f.registration} / {f.callsign}")

print("--- Testing get_flights(registration=...) ---")
for r in regs:
    try:
        f_reg = fr_api.get_flights(registration=r)
        print(f"Search for {r}: {len(f_reg)} found")
        for f in f_reg:
            print(f"  -> {f.registration} / {f.callsign}")
    except Exception as e:
        print(f"Error searching for {r}: {e}")
