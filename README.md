## V20 fix
- Fixed the appointment-booking JavaScript response variable and added a cache-busting version to the app.js script so stale browser JavaScript cannot continue producing the `r is not defined` popup.

# CODESTORM Patient Case Taking Software

## Current patient workflow
1. A patient creates an account without selecting a doctor.
2. The patient can log in directly with email/password or Google.
3. The patient chooses a doctor only when booking the **first appointment**.
4. That first appointment registers the patient to the selected doctor.
5. After registration, the patient can book future appointments only with the registered doctor.
6. A patient can have only **one appointment per calendar date**.
7. After submitting an appointment, the patient has **5 minutes** to edit the appointment date/time and reason for visit.
8. After 5 minutes, the appointment edit option is disabled and the reason for visit is hidden from the appointment listing.
9. The 5-minute restriction is enforced by the backend, not only by the browser UI.

## Doctor visibility
- Each doctor sees only patients registered to that doctor.
- A patient becomes registered to a doctor when the patient books the first appointment with that doctor.
- Admin can view all patients.

## Google sign-in
- Patient Google registration does not ask for a doctor.
- Patient Google login does not ask for a doctor.
- Doctor Google sign-in still requires selecting the doctor profile because it links Google to that doctor account.

## Run on Windows
```bat
python -m venv .venv
.venv\\Scripts\\activate
pip install -r requirements.txt
python app.py
```
Open http://localhost:8000

## Patient referral information
- Patient registration now has a **compulsory** question: “Were you referred by another doctor? Yes/No”.
- The referring doctor name field is hidden until **Yes** is selected, and then the name is required.
- Google-registered patients must complete this referral information in the patient portal before booking an appointment.
- The referral information is visible in the patient profile and is enforced by the backend.

## Doctor-to-doctor case referral
- A doctor can open a case and choose **Refer to Another Doctor**.
- The referring doctor selects the receiving doctor and can enter a referral reason.
- On confirmation, the case ownership is transferred to the receiving doctor.
- The patient registration is also transferred to the receiving doctor.
- The referring doctor retains view access to the referred case but cannot edit or delete it.
- Only the receiving doctor can manage the referred case; the patient can still see their own case and referral history.
- Referral history records the referring doctor, receiving doctor, patient, case, reason and timestamp.


## Referral access rule
- When Doctor A refers a case to Doctor B, Doctor B becomes the current case owner.
- Doctor A retains view access to that referred case, including case list and case detail.
- Doctor A cannot edit or delete the referred case after transfer.
- Doctor B can view, edit and refer the case further.
- Other doctors cannot access the case.

### Referral restriction
- A receiving doctor cannot refer the case back to the same doctor who made the immediately preceding referral.


## Patient-doctor messaging
The project includes a simple local messaging system between a patient and the patient's currently registered doctor. Patients can send messages from the Messages section; doctors can select their registered patients and reply. After a doctor-to-doctor referral, the patient's registered doctor is updated to the receiving doctor, so new patient messages automatically go to that doctor. The original referring doctor retains case view access but is not an active patient-messaging doctor. Messages are stored in SQLite with timestamps and unread/read state. This is ordinary application messaging, not an emergency or diagnostic service.
- After Doctor A refers a case to Doctor B, Doctor A can open the referred patient from Patient Messages or the referred case and view the read-only conversation between Doctor B and the patient. Doctor A cannot send or modify messages in that conversation.
- The conversation shown to the referring doctor is the patient's current registered-doctor thread, so after referral it is specifically the Doctor B ↔ patient conversation.


### Appointment Referral Details (V12)
During appointment booking, the patient must select whether they were referred by another doctor. If Yes is selected, the referring doctor name is required. These details are stored with the appointment and are visible to the doctor. The patient can correct them during the existing 5-minute appointment edit window.
