"""
Database initialization script.

Creates the SQLite database and the `appointments` table. Run once before the
first start (Railway runs it automatically via the release/start command, but
it is also safe to run manually):

    python init_db.py

Optionally seeds a few sample rows with:

    python init_db.py --seed
"""

import sys
from datetime import date, time as dtime, timedelta, datetime

from app import app
from models import db, Appointment, AppointmentStatus, AppointmentSource


def init(seed=False):
    with app.app_context():
        db.create_all()
        print("✅ Base de datos inicializada (tabla 'appointments' creada).")

        if seed:
            if Appointment.query.count() > 0:
                print("ℹ️  La base de datos ya contiene datos; no se añaden ejemplos.")
                return
            tomorrow = date.today() + timedelta(days=1)
            samples = [
                Appointment(
                    customer_name='Juan Pérez', customer_phone='+34611111111',
                    location='Lepe', work_type='Instalación de armario',
                    duration_hours=1.5, appointment_date=tomorrow,
                    start_time=dtime(9, 0), end_time=dtime(10, 30),
                    status=AppointmentStatus.CONFIRMED,
                    source=AppointmentSource.LEROY_MERLIN, wo_reference='WO-123',
                    notes='Calle Mayor 10'),
                Appointment(
                    customer_name='Ana López', customer_phone='+34622222222',
                    location='Ayamonte', work_type='Montaje de cocina',
                    duration_hours=2.0, appointment_date=tomorrow,
                    start_time=dtime(12, 0), end_time=dtime(14, 0),
                    status=AppointmentStatus.CONFIRMED,
                    source=AppointmentSource.LEROY_MERLIN, wo_reference='WO-456',
                    notes='Av. del Mar 5'),
                Appointment(
                    customer_name='Trabajo personal', location='Huelva',
                    work_type='Reforma baño (personal)', duration_hours=3.0,
                    appointment_date=tomorrow + timedelta(days=1),
                    start_time=dtime(8, 0), end_time=dtime(11, 0),
                    status=AppointmentStatus.CONFIRMED,
                    source=AppointmentSource.PERSONAL, notes='Cliente particular'),
            ]
            db.session.add_all(samples)
            db.session.commit()
            print(f"🌱 Añadidos {len(samples)} registros de ejemplo.")


if __name__ == '__main__':
    init(seed='--seed' in sys.argv)
