# ✈️ Skyguard Ops Control

Professional aircraft monitoring dashboard for tracking specific B757 freighter fleets. Combines live FlightRadar24 tracking with internal flight schedules (via Aviabit).

## 🌟 Features
- **Live Tracking**: Real-time positioning, altitude, and speed via FlightRadar24 API.
- **Flight Schedule Sync**: Automatic integration with Aviabit API to show flight numbers and routes.
- **Advanced ETA**: Real-time calculation of Estimated Time of Arrival based on ground speed.
- **Glassmorphism UI**: High-end dashboard design with smooth map animations.
- **Notifications**: Automated alerts for takeoff and landing events.
- **Time Sync**: Integrated UTC clock for aviation standard operations.

## 🚀 Quick Start

### 1. Prerequisites
- Python 3.8+
- Active connection to FlightRadar24 and Aviabit.

### 2. Installations
```bash
pip install -r requirements.txt
```

### 3. Configuration
Rename `config.example.json` to `config.json` and fill in your credentials:
- `aviabit`: Your sky_ops login/password.
- `aircraft`: List of registrations to track.
- `telegram`: Bot token for alerts (optional).

### 4. Run
```bash
python app.py
```
Open **[http://localhost:5050](http://localhost:5050)** in your browser.

## 💼 Commercial & Professional Use
This project is refactored for portability. It can be deployed on a VPS (Virtual Private Server) to provide 24/7 monitoring for fleet managers.

---
*Created for Skyguard Fleet Operations.*
