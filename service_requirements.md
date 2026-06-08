# Municipal Service Data Requirements

This file defines what information the AI assistant should collect for each service category.
Each section maps directly to a `selectedService` value from the frontend.
If the citizen cannot provide certain fields, proceed without them — the Maestro process
will validate completeness and return an appropriate status (OK / data missing / pending).

---

## Identity Document (ID Card)

### Required Information
Collect as many of these as possible before submitting. Always ask politely — never block the
conversation if a field is missing.

| Field | Notes |
|---|---|
| Full name | First name and surname as they appear on official records |
| Date of birth | DD/MM/YYYY |
| Place of birth | City and country |
| Nationality / Citizenship | As registered |
| Current residential address | Street, city, postal code, country |
| Contact phone number | |
| Contact email address | |
| Reason for application | `new` · `renewal` · `replacement` |
| (If renewal) Current ID number | Document number on existing card |
| (If renewal) Current ID expiry date | |
| (If replacement) Reason | `lost` · `stolen` · `damaged` |
| (If stolen) Police report reference | Report number from local police station |

### Supporting Documents to Upload
Ask the citizen if they can upload any of these. They are **not mandatory** to call Maestro.

- Proof of address (utility bill, bank statement — issued within the last 3 months)
- Proof of citizenship or birth certificate
- Passport-style photo (alternatively taken at the municipal office  or using capture photo with webcam)
- Current ID card (if renewal or replacement of damaged card)
- Police report (if card was stolen)
- Sworn statement of loss (if card was lost)

### Process Notes
- Appointments may be required for biometric capture (fingerprints/photo). Advise the citizen to
  book after submission is confirmed.
- Processing time is typically 10–15 working days for standard applications.
- Expedited processing (3–5 days) may be available for an additional fee.

---

## Passport

### Required Information

| Field | Notes |
|---|---|
| Full name | Exactly as it should appear in the passport |
| Date of birth | DD/MM/YYYY |
| Place of birth | City and country |
| Nationality | As registered |
| Current residential address | Street, city, postal code, country |
| Contact phone number | |
| Contact email address | |
| Reason for application | `new` · `renewal` · `emergency` |
| (If renewal) Current passport number | |
| (If renewal) Current passport expiry date | |
| (If emergency) Planned departure date | Used to determine urgency |
| (If emergency) Reason for urgency | E.g. medical travel, bereavement, business |

### Supporting Documents to Upload

- Birth certificate or naturalization certificate
- Proof of address (issued within the last 3 months)
- Two passport-style photographs (or taken at the municipal office)
- Current passport (if renewal)
- Police report (if passport was lost or stolen)
- Proof of imminent travel (flight booking, visa appointment letter) — required for emergency processing

### Process Notes
- Standard processing: 15–20 working days.
- Expedited processing (5–7 days): available on request, subject to fee.
- Emergency same-day processing: requires documented proof of urgent travel.
- Minors (under 18) require consent from both guardians; one guardian must attend in person.

---

## Work Permit

### Required Information

| Field | Notes |
|---|---|
| Full name of applicant | |
| Date of birth | DD/MM/YYYY |
| Nationality / Country of origin | |
| Current residential address | |
| Passport number | |
| Passport expiry date | |
| Type of application | `new` · `renewal` · `change of employer` |
| Employer name | |
| Employer address | |
| Job title / Position | |
| Type of employment | `full-time` · `part-time` · `seasonal` · `contractor` |
| Intended start date | DD/MM/YYYY |
| Intended end date | DD/MM/YYYY (or `open-ended`) |
| (If renewal) Current permit number | |
| (If renewal) Current permit expiry date | |
| (If change of employer) Previous employer name | |

### Supporting Documents to Upload

- Valid passport (copy of bio-data page)
- Employment contract or confirmed job offer letter
- Employer business registration / operating license
- Proof of qualifications or professional certifications
- Medical fitness certificate (if required by the specific sector)
- Current work permit copy (if renewal or change of employer)
- CV / résumé (helpful but not mandatory)

### Process Notes
- Processing time: 20–30 working days from receipt of complete application.
- Some sectors (healthcare, security, education) require additional background checks.
- The employer may need to provide a Labour Market Impact Assessment (LMIA) or equivalent,
  depending on the position. Advise the citizen to confirm this with their employer.
- A work permit does not automatically grant the right to reside — a separate residence
  registration may be required.

---

## Construction Permit

### Required Information

| Field | Notes |
|---|---|
| Applicant full name or company name | If company, include registered company number |
| Contact address | |
| Contact phone number | |
| Contact email address | |
| Property address | Street, city, postal code |
| Cadastral / Plot reference number | Land registry parcel ID (if known) |
| Type of works | `new construction` · `extension` · `renovation` · `demolition` · `change of use` |
| Description of works | Brief plain-language summary of what is planned |
| Estimated total floor area affected | In square metres (m²) |
| Estimated project start date | DD/MM/YYYY |
| Estimated project duration | E.g. "6 months", "18 months" |
| Architect or technical designer name | |
| Architect licence / registration number | |
| General contractor name | (if already appointed) |

### Supporting Documents to Upload

- Proof of property ownership (land deed, title certificate)
- Architectural/structural drawings and floor plans (PDF)
- Site plan showing plot boundaries and neighbouring structures
- Location/zoning map
- Structural engineer's report (required for structural modifications)
- Environmental impact statement (required for projects exceeding 500 m² or in protected zones)
- Previous building permit (if renovating or extending an existing structure)
- Fire safety compliance declaration (for commercial or multi-dwelling projects)

### Process Notes
- Zoning compliance is verified before the permit is issued. The citizen should confirm the
  intended use is allowed under the local zoning plan (PDM/PDR).
- Processing time: 30–60 working days depending on project complexity.
- Neighbours within a defined radius must be formally notified for new constructions
  and major extensions; the municipality handles this notification after submission.
- Inspections are required at foundation, structural, and completion stages.
- Work must not commence before the permit is officially issued and displayed on site.
