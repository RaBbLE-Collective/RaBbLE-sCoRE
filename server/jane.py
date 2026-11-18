import os
from datetime import datetime, timezone

import httpx


JANE_BASE_URL = os.getenv("JANE_BASE_URL", "https://api.jane.app/api/v2")


MOCK_PATIENTS = {
    "p1": {
        "id": "p1",
        "first_name": "Sarah",
        "last_name": "R.",
        "date_of_birth": "1987-03-04",
        "email": "sarah.r@example.com",
        "phone": "555-0101",
        "chart_number": "PT-1001",
        "diagnosis": "L4-L5 disc herniation",
        "sessions_used": 7,
        "sessions_authorized": 12,
    },
    "p2": {
        "id": "p2",
        "first_name": "Marcus",
        "last_name": "K.",
        "date_of_birth": "1991-11-12",
        "email": "marcus.k@example.com",
        "phone": "555-0102",
        "chart_number": "PT-1002",
        "diagnosis": "Post-op ACL rehab",
        "sessions_used": 4,
        "sessions_authorized": 16,
    },
    "p3": {
        "id": "p3",
        "first_name": "Janet",
        "last_name": "L.",
        "date_of_birth": "1975-07-18",
        "email": "janet.l@example.com",
        "phone": "555-0103",
        "chart_number": "PT-1003",
        "diagnosis": "Rotator cuff tendinopathy",
        "sessions_used": 3,
        "sessions_authorized": 8,
    },
    "p4": {
        "id": "p4",
        "first_name": "Tom",
        "last_name": "D.",
        "date_of_birth": "1968-01-09",
        "email": "tom.d@example.com",
        "phone": "555-0104",
        "chart_number": "PT-1004",
        "diagnosis": "Cervicogenic headache workup",
        "sessions_used": 1,
        "sessions_authorized": 6,
    },
}


MOCK_APPOINTMENTS = {
    "p1": [
        {
            "id": "appt_p1_1",
            "patient_id": "p1",
            "staff_id": "jane_staff_001",
            "start_at": "2026-04-23T14:00:00-04:00",
            "duration_minutes": 60,
            "appointment_type": "Follow-up",
            "status": "booked",
        },
        {
            "id": "appt_p1_2",
            "patient_id": "p1",
            "staff_id": "jane_staff_001",
            "start_at": "2026-04-30T14:00:00-04:00",
            "duration_minutes": 60,
            "appointment_type": "Follow-up",
            "status": "booked",
        },
    ],
    "p2": [
        {
            "id": "appt_p2_1",
            "patient_id": "p2",
            "staff_id": "jane_staff_001",
            "start_at": "2026-04-29T09:00:00-04:00",
            "duration_minutes": 60,
            "appointment_type": "ACL rehab",
            "status": "booked",
        }
    ],
    "p3": [
        {
            "id": "appt_p3_1",
            "patient_id": "p3",
            "staff_id": "jane_staff_001",
            "start_at": "2026-04-24T10:00:00-04:00",
            "duration_minutes": 45,
            "appointment_type": "Shoulder follow-up",
            "status": "booked",
        }
    ],
    "p4": [
        {
            "id": "appt_p4_1",
            "patient_id": "p4",
            "staff_id": "jane_staff_001",
            "start_at": "2026-04-22T11:00:00-04:00",
            "duration_minutes": 60,
            "appointment_type": "Initial evaluation",
            "status": "completed",
        }
    ],
}


MOCK_CHART_ENTRIES = {
    "p1": [
        {
            "id": "chart_p1_1",
            "patient_id": "p1",
            "appointment_id": "appt_p1_1",
            "created_at": "2026-04-21T15:10:00-04:00",
            "sections": {
                "subjective": "Reports 60% pain reduction since last week. Mild stiffness in the morning.",
                "objective": "Improved hip flexor strength. Tolerated lumbar traction and core stabilization.",
                "assessment": "Progressing well with reduced irritability and better activity tolerance.",
                "plan": "Continue current protocol and reassess traction dosage next visit.",
            },
            "billing_codes": ["97110", "97012"],
        }
    ],
    "p2": [
        {
            "id": "chart_p2_1",
            "patient_id": "p2",
            "appointment_id": "appt_p2_1",
            "created_at": "2026-04-20T09:45:00-04:00",
            "sections": {
                "subjective": "Knee feels more stable descending stairs.",
                "objective": "Single-leg squat depth improved. No swelling after session.",
                "assessment": "On track for week 4 ACL goals.",
                "plan": "Progress closed-chain strength and introduce controlled lateral work.",
            },
            "billing_codes": ["97110", "97530"],
        }
    ],
    "p3": [
        {
            "id": "chart_p3_1",
            "patient_id": "p3",
            "appointment_id": "appt_p3_1",
            "created_at": "2026-04-17T10:30:00-04:00",
            "sections": {
                "subjective": "Shoulder pain improving but still aggravated by overhead reach.",
                "objective": "Abduction tolerance improved to 120 degrees.",
                "assessment": "Moderate improvement with residual tendon irritability.",
                "plan": "Continue scapular stabilization and graded overhead loading.",
            },
            "billing_codes": ["97110"],
        }
    ],
    "p4": [
        {
            "id": "chart_p4_1",
            "patient_id": "p4",
            "appointment_id": "appt_p4_1",
            "created_at": "2026-04-22T11:50:00-04:00",
            "sections": {
                "subjective": "Reports recurring headache pattern associated with cervical stiffness.",
                "objective": "Limited cervical rotation and suboccipital tenderness.",
                "assessment": "Presentation consistent with cervicogenic headache.",
                "plan": "Begin manual therapy and deep neck flexor progression.",
            },
            "billing_codes": ["97140", "97110"],
        }
    ],
}


MOCK_TREATMENT_PLANS = {
    "p1": {
        "id": "plan_p1",
        "patient_id": "p1",
        "diagnosis": "L4-L5 disc herniation",
        "summary": "Lumbar traction, core stabilization, graded return to lifting.",
        "sessions_used": 7,
        "sessions_authorized": 12,
        "sessions_remaining": 5,
        "home_program": [
            {"name": "Dead bug", "sets": 3, "reps": 8},
            {"name": "Bird dog", "sets": 3, "reps": 6},
        ],
    },
    "p2": {
        "id": "plan_p2",
        "patient_id": "p2",
        "diagnosis": "Post-op ACL rehab",
        "summary": "Progressive quad strengthening and neuromuscular control.",
        "sessions_used": 4,
        "sessions_authorized": 16,
        "sessions_remaining": 12,
        "home_program": [
            {"name": "Sit-to-stand", "sets": 3, "reps": 10},
            {"name": "Step-up", "sets": 3, "reps": 8},
        ],
    },
    "p3": {
        "id": "plan_p3",
        "patient_id": "p3",
        "diagnosis": "Rotator cuff tendinopathy",
        "summary": "Scapular control and graded overhead tolerance.",
        "sessions_used": 3,
        "sessions_authorized": 8,
        "sessions_remaining": 5,
        "home_program": [
            {"name": "Band row", "sets": 3, "reps": 12},
            {"name": "Wall slide", "sets": 3, "reps": 8},
        ],
    },
    "p4": {
        "id": "plan_p4",
        "patient_id": "p4",
        "diagnosis": "Cervicogenic headache workup",
        "summary": "Manual therapy and cervical stabilization trial.",
        "sessions_used": 1,
        "sessions_authorized": 6,
        "sessions_remaining": 5,
        "home_program": [
            {"name": "Chin tuck", "sets": 3, "reps": 10},
            {"name": "Thoracic extension", "sets": 2, "reps": 8},
        ],
    },
}


MOCK_STAFF_AVAILABILITY = [
    {"staff_id": "jane_staff_001", "start_at": "2026-04-24T09:00:00-04:00", "duration_minutes": 60},
    {"staff_id": "jane_staff_001", "start_at": "2026-04-24T11:00:00-04:00", "duration_minutes": 60},
    {"staff_id": "jane_staff_001", "start_at": "2026-04-25T14:00:00-04:00", "duration_minutes": 60},
]


def _is_mock_mode() -> bool:
    key = os.getenv("JANE_API_KEY", "").strip().lower()
    return key in {"", "mock"}


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {os.getenv('JANE_API_KEY', '')}",
        "Content-Type": "application/json",
    }


async def _jane_get(path: str, params: dict | None = None):
    async with httpx.AsyncClient() as client:
        response = await client.get(
            f"{JANE_BASE_URL}{path}",
            headers=_headers(),
            params=params,
            timeout=30,
        )
    response.raise_for_status()
    return response.json()


async def _jane_post(path: str, payload: dict):
    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{JANE_BASE_URL}{path}",
            headers=_headers(),
            json=payload,
            timeout=30,
        )
    response.raise_for_status()
    return response.json()


def _full_name(patient: dict) -> str:
    return f"{patient['first_name']} {patient['last_name']}".strip()


def _sort_appointments(appointments: list[dict]) -> list[dict]:
    return sorted(appointments, key=lambda item: item["start_at"])


def _format_appointment(appointment: dict | None) -> str:
    if not appointment:
        return "None scheduled"
    return f"{appointment['start_at']} ({appointment.get('appointment_type', 'Visit')})"


def _format_home_program(items: list[dict]) -> str:
    if not items:
        return "None recorded"
    return "; ".join(
        f"{item['name']} {item.get('sets', '?')}x{item.get('reps', '?')}"
        for item in items
    )


async def list_patients() -> list[dict]:
    if _is_mock_mode():
        return [
            {
                "id": patient["id"],
                "name": _full_name(patient),
                "first_name": patient["first_name"],
                "last_name": patient["last_name"],
                "diagnosis": patient["diagnosis"],
            }
            for patient in MOCK_PATIENTS.values()
        ]
    data = await _jane_get("/patients")
    return data.get("patients", data)


async def get_patient(patient_id: str) -> dict:
    if _is_mock_mode():
        patient = MOCK_PATIENTS.get(patient_id)
        if not patient:
            raise ValueError(f"Unknown mock patient '{patient_id}'")
        return patient
    return await _jane_get(f"/patients/{patient_id}")


async def get_appointments(patient_id: str) -> list[dict]:
    if _is_mock_mode():
        return _sort_appointments(MOCK_APPOINTMENTS.get(patient_id, []))
    data = await _jane_get("/appointments", params={"patient_id": patient_id})
    return data.get("appointments", data)


async def get_chart_entries(patient_id: str) -> list[dict]:
    if _is_mock_mode():
        return MOCK_CHART_ENTRIES.get(patient_id, [])
    data = await _jane_get("/chart_entries", params={"patient_id": patient_id})
    return data.get("chart_entries", data)


async def get_treatment_plan(patient_id: str) -> dict | None:
    if _is_mock_mode():
        return MOCK_TREATMENT_PLANS.get(patient_id)
    data = await _jane_get("/treatment_plans", params={"patient_id": patient_id, "status": "active"})
    plans = data.get("treatment_plans", data)
    return plans[0] if plans else None


async def get_staff_availability(start_date: str | None = None, end_date: str | None = None) -> list[dict]:
    if _is_mock_mode():
        return MOCK_STAFF_AVAILABILITY
    data = await _jane_get("/staff/jane_staff_001/availability", params={"start_date": start_date, "end_date": end_date})
    return data.get("availability", data)


async def find_patient_by_email(email: str) -> dict | None:
    if _is_mock_mode():
        lowered = email.strip().lower()
        for patient in MOCK_PATIENTS.values():
            if patient["email"].lower() == lowered:
                return patient
        return None
    patients = await _jane_get("/patients", params={"email": email})
    items = patients.get("patients", patients)
    return items[0] if items else None


async def execute_action(action_type: str, payload: dict) -> dict:
    if _is_mock_mode():
        return {
            "id": f"mock_{action_type}_{payload.get('patient_id', 'resource')}",
            "status": "ok",
            "action_type": action_type,
            "payload": payload,
            "mode": "mock",
            "executed_at": datetime.now(timezone.utc).isoformat(),
        }

    if action_type == "book_appointment":
        return await _jane_post("/appointments", payload)
    if action_type == "save_soap_note":
        return await _jane_post("/chart_entries", payload)
    if action_type == "save_exercise_plan":
        plan_id = payload.get("plan_id")
        if not plan_id:
            raise ValueError("save_exercise_plan requires plan_id in live mode")
        async with httpx.AsyncClient() as client:
            response = await client.put(
                f"{JANE_BASE_URL}/treatment_plans/{plan_id}",
                headers=_headers(),
                json=payload,
                timeout=30,
            )
        response.raise_for_status()
        return response.json()
    raise ValueError(f"Unsupported live Jane action '{action_type}'")


async def build_patient_context(patient_id: str) -> str:
    patient, appointments, plan = await _gather_patient_context(patient_id)
    next_appointment = next(
        (appointment for appointment in appointments if appointment.get("status") != "completed"),
        None,
    )
    return (
        f"Patient: {patient['first_name']}\n"
        f"Next appointment: {_format_appointment(next_appointment)}\n"
        f"Diagnosis: {plan.get('diagnosis', 'Not recorded') if plan else 'Not recorded'}\n"
        f"Sessions remaining: {plan.get('sessions_remaining', 'Unknown') if plan else 'Unknown'}\n"
        f"Home exercises: {_format_home_program(plan.get('home_program', []) if plan else [])}"
    ).strip()


async def build_practitioner_context(patient_id: str, appointment_id: str | None = None) -> str:
    patient, appointments, plan = await _gather_patient_context(patient_id)
    chart_entries = await get_chart_entries(patient_id)
    last_note = chart_entries[0] if chart_entries else None
    target_appointment = next((item for item in appointments if item["id"] == appointment_id), None) if appointment_id else None
    next_appointment = target_appointment or next(
        (appointment for appointment in appointments if appointment.get("status") != "completed"),
        None,
    )
    return (
        f"Patient: {_full_name(patient)}, DOB: {patient['date_of_birth']}\n"
        f"Diagnosis: {plan.get('diagnosis', 'Not recorded') if plan else 'Not recorded'}\n"
        f"Auth: {plan.get('sessions_used', '?') if plan else '?'} of {plan.get('sessions_authorized', '?') if plan else '?'} sessions used\n"
        f"Last visit: {last_note['created_at']} — {last_note['sections'].get('assessment', '') if last_note else 'None'}\n"
        f"Next appointment: {_format_appointment(next_appointment)}\n"
        f"Current plan: {plan.get('summary', 'None') if plan else 'None'}"
    ).strip()


async def _gather_patient_context(patient_id: str):
    patient = await get_patient(patient_id)
    appointments = await get_appointments(patient_id)
    plan = await get_treatment_plan(patient_id)
    return patient, appointments, plan
