import json
from schedule_service import SkyguardScheduleService

with open('config.json', encoding='utf-8') as f:
    config = json.load(f)

svc = SkyguardScheduleService(config['aviabit']['username'], config['aviabit']['password'])
plan = svc.get_flight_plan(search_regs=['UK-75058'])

if not plan:
    print("NO PLAN RETURNED DUMPING ALL:")
    plan = svc.get_flight_plan()

for f in plan:
    if f.get('pln', '') and '75058' in f.get('pln', ''):
        print(f"75058 flight: {f.get('flight')} - T0: {f.get('dateTakeoff')}")
