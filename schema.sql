-- AI Hospital Digital Twin & Operations Platform
-- Synthetic-data MVP schema. PostgreSQL-compatible.
-- All records must be synthetic. This is not a clinical system.

CREATE TABLE departments (
  department_id VARCHAR(20) PRIMARY KEY,
  department_name VARCHAR(100) NOT NULL,
  department_name_ar VARCHAR(100) NOT NULL,
  department_type VARCHAR(30) NOT NULL CHECK (department_type IN ('ER','ICU','WARD','OR','CLINIC','IMAGING','LAB')),
  is_active BOOLEAN NOT NULL DEFAULT TRUE
);

CREATE TABLE rooms (
  room_id VARCHAR(20) PRIMARY KEY,
  department_id VARCHAR(20) NOT NULL REFERENCES departments(department_id),
  room_name VARCHAR(100) NOT NULL,
  room_type VARCHAR(30) NOT NULL,
  is_active BOOLEAN NOT NULL DEFAULT TRUE
);

CREATE TABLE beds (
  bed_id VARCHAR(20) PRIMARY KEY,
  room_id VARCHAR(20) NOT NULL REFERENCES rooms(room_id),
  bed_type VARCHAR(30) NOT NULL,
  status VARCHAR(20) NOT NULL CHECK (status IN ('AVAILABLE','OCCUPIED','CLEANING','OUT_OF_SERVICE')),
  is_isolation_capable BOOLEAN NOT NULL DEFAULT FALSE
);

CREATE TABLE patients (
  patient_id VARCHAR(20) PRIMARY KEY,
  synthetic_age INTEGER NOT NULL CHECK (synthetic_age BETWEEN 0 AND 110),
  synthetic_sex VARCHAR(20) NOT NULL CHECK (synthetic_sex IN ('F','M','OTHER','UNKNOWN')),
  synthetic_cohort VARCHAR(40) NOT NULL,
  created_at TIMESTAMP NOT NULL
);

CREATE TABLE visits (
  visit_id VARCHAR(20) PRIMARY KEY,
  patient_id VARCHAR(20) NOT NULL REFERENCES patients(patient_id),
  arrival_time TIMESTAMP NOT NULL,
  arrival_source VARCHAR(30) NOT NULL,
  acuity_level INTEGER CHECK (acuity_level BETWEEN 1 AND 5),
  service_line VARCHAR(40) NOT NULL,
  disposition VARCHAR(30) NOT NULL CHECK (disposition IN ('DISCHARGED','ADMITTED','TRANSFERRED','LEFT_WITHOUT_BEING_SEEN')),
  departure_time TIMESTAMP,
  wait_minutes INTEGER CHECK (wait_minutes >= 0),
  length_of_stay_hours NUMERIC(8,2) CHECK (length_of_stay_hours >= 0)
);

CREATE TABLE bed_stays (
  bed_stay_id VARCHAR(20) PRIMARY KEY,
  visit_id VARCHAR(20) NOT NULL REFERENCES visits(visit_id),
  bed_id VARCHAR(20) NOT NULL REFERENCES beds(bed_id),
  start_time TIMESTAMP NOT NULL,
  end_time TIMESTAMP,
  transfer_reason VARCHAR(100),
  CHECK (end_time IS NULL OR end_time >= start_time)
);

CREATE TABLE er_events (
  er_event_id VARCHAR(20) PRIMARY KEY,
  visit_id VARCHAR(20) NOT NULL REFERENCES visits(visit_id),
  event_time TIMESTAMP NOT NULL,
  event_type VARCHAR(40) NOT NULL,
  queue_minutes INTEGER CHECK (queue_minutes >= 0),
  notes_code VARCHAR(40)
);

CREATE TABLE staff (
  staff_id VARCHAR(20) PRIMARY KEY,
  department_id VARCHAR(20) NOT NULL REFERENCES departments(department_id),
  role VARCHAR(30) NOT NULL CHECK (role IN ('DOCTOR','NURSE','TECHNICIAN','ADMIN','SURGEON','ANESTHETIST')),
  skill_group VARCHAR(40) NOT NULL,
  employment_type VARCHAR(20) NOT NULL CHECK (employment_type IN ('FULL_TIME','PART_TIME','CONTRACT')),
  max_hours_per_week INTEGER NOT NULL CHECK (max_hours_per_week BETWEEN 1 AND 80),
  hourly_cost_egp NUMERIC(10,2) NOT NULL CHECK (hourly_cost_egp >= 0),
  is_active BOOLEAN NOT NULL DEFAULT TRUE
);

CREATE TABLE staff_shifts (
  shift_id VARCHAR(20) PRIMARY KEY,
  staff_id VARCHAR(20) NOT NULL REFERENCES staff(staff_id),
  department_id VARCHAR(20) NOT NULL REFERENCES departments(department_id),
  shift_start TIMESTAMP NOT NULL,
  shift_end TIMESTAMP NOT NULL,
  status VARCHAR(20) NOT NULL CHECK (status IN ('SCHEDULED','COMPLETED','ABSENT','CANCELLED')),
  CHECK (shift_end > shift_start)
);

CREATE TABLE staff_assignments (
  assignment_id VARCHAR(20) PRIMARY KEY,
  staff_id VARCHAR(20) NOT NULL REFERENCES staff(staff_id),
  department_id VARCHAR(20) NOT NULL REFERENCES departments(department_id),
  assignment_start TIMESTAMP NOT NULL,
  assignment_end TIMESTAMP NOT NULL,
  assignment_type VARCHAR(30) NOT NULL,
  workload_units NUMERIC(8,2) NOT NULL DEFAULT 1,
  source_scenario_id VARCHAR(30),
  CHECK (assignment_end > assignment_start)
);

CREATE TABLE operating_rooms (
  or_id VARCHAR(20) PRIMARY KEY,
  department_id VARCHAR(20) NOT NULL REFERENCES departments(department_id),
  room_name VARCHAR(100) NOT NULL,
  specialty VARCHAR(40) NOT NULL,
  daily_capacity_hours NUMERIC(5,2) NOT NULL CHECK (daily_capacity_hours > 0),
  is_active BOOLEAN NOT NULL DEFAULT TRUE
);

CREATE TABLE surgeries (
  surgery_id VARCHAR(20) PRIMARY KEY,
  visit_id VARCHAR(20) NOT NULL REFERENCES visits(visit_id),
  or_id VARCHAR(20) NOT NULL REFERENCES operating_rooms(or_id),
  specialty VARCHAR(40) NOT NULL,
  planned_start TIMESTAMP NOT NULL,
  planned_duration_minutes INTEGER NOT NULL CHECK (planned_duration_minutes > 0),
  actual_start TIMESTAMP,
  actual_end TIMESTAMP,
  urgency VARCHAR(20) NOT NULL CHECK (urgency IN ('EMERGENCY','URGENT','ELECTIVE')),
  status VARCHAR(20) NOT NULL CHECK (status IN ('SCHEDULED','COMPLETED','DELAYED','CANCELLED')),
  expected_contribution_margin_egp NUMERIC(12,2) NOT NULL DEFAULT 0,
  contribution_margin_egp NUMERIC(12,2) NOT NULL DEFAULT 0,
  CHECK (actual_end IS NULL OR actual_start IS NULL OR actual_end >= actual_start)
);

CREATE TABLE appointments (
  appointment_id VARCHAR(20) PRIMARY KEY,
  patient_id VARCHAR(20) NOT NULL REFERENCES patients(patient_id),
  department_id VARCHAR(20) NOT NULL REFERENCES departments(department_id),
  scheduled_start TIMESTAMP NOT NULL,
  duration_minutes INTEGER NOT NULL CHECK (duration_minutes > 0),
  appointment_type VARCHAR(30) NOT NULL,
  status VARCHAR(20) NOT NULL CHECK (status IN ('SCHEDULED','COMPLETED','CANCELLED','NO_SHOW','RESCHEDULED')),
  wait_minutes INTEGER CHECK (wait_minutes >= 0)
);

CREATE TABLE resources (
  resource_id VARCHAR(20) PRIMARY KEY,
  department_id VARCHAR(20) NOT NULL REFERENCES departments(department_id),
  resource_name VARCHAR(100) NOT NULL,
  resource_type VARCHAR(40) NOT NULL,
  quantity INTEGER NOT NULL CHECK (quantity >= 0),
  status VARCHAR(20) NOT NULL CHECK (status IN ('AVAILABLE','LIMITED','OUT_OF_SERVICE'))
);

CREATE TABLE resource_bookings (
  booking_id VARCHAR(20) PRIMARY KEY,
  resource_id VARCHAR(20) NOT NULL REFERENCES resources(resource_id),
  visit_id VARCHAR(20) REFERENCES visits(visit_id),
  booking_start TIMESTAMP NOT NULL,
  booking_end TIMESTAMP NOT NULL,
  quantity_reserved INTEGER NOT NULL CHECK (quantity_reserved > 0),
  status VARCHAR(20) NOT NULL CHECK (status IN ('PLANNED','IN_USE','COMPLETED','CANCELLED')),
  CHECK (booking_end > booking_start)
);

CREATE TABLE cost_rates (
  cost_rate_id VARCHAR(20) PRIMARY KEY,
  cost_category VARCHAR(40) NOT NULL,
  department_id VARCHAR(20) REFERENCES departments(department_id),
  unit VARCHAR(30) NOT NULL,
  rate_egp NUMERIC(12,2) NOT NULL CHECK (rate_egp >= 0),
  effective_from DATE NOT NULL,
  effective_to DATE
);

CREATE TABLE financial_daily (
  financial_id VARCHAR(20) PRIMARY KEY,
  metric_date DATE NOT NULL,
  department_id VARCHAR(20) NOT NULL REFERENCES departments(department_id),
  revenue_egp NUMERIC(14,2) NOT NULL DEFAULT 0,
  labor_cost_egp NUMERIC(14,2) NOT NULL DEFAULT 0,
  supply_cost_egp NUMERIC(14,2) NOT NULL DEFAULT 0,
  overtime_cost_egp NUMERIC(14,2) NOT NULL DEFAULT 0,
  cancelled_procedure_risk_egp NUMERIC(14,2) NOT NULL DEFAULT 0,
  UNIQUE(metric_date, department_id)
);

CREATE TABLE daily_metrics (
  metric_id VARCHAR(20) PRIMARY KEY,
  metric_date DATE NOT NULL,
  department_id VARCHAR(20) NOT NULL REFERENCES departments(department_id),
  arrivals_count INTEGER NOT NULL DEFAULT 0,
  admissions_count INTEGER NOT NULL DEFAULT 0,
  discharges_count INTEGER NOT NULL DEFAULT 0,
  occupied_beds INTEGER NOT NULL DEFAULT 0,
  staffed_beds INTEGER NOT NULL DEFAULT 0,
  occupancy_rate NUMERIC(6,4),
  avg_wait_minutes NUMERIC(8,2),
  p90_wait_minutes NUMERIC(8,2),
  er_queue_peak INTEGER,
  surgeries_scheduled INTEGER,
  surgeries_completed INTEGER,
  or_utilization_rate NUMERIC(6,4),
  staff_absence_count INTEGER,
  overtime_hours NUMERIC(8,2),
  CHECK (arrivals_count >= 0 AND admissions_count >= 0 AND discharges_count >= 0)
);

CREATE TABLE forecast_results (
  forecast_id VARCHAR(20) PRIMARY KEY,
  created_at TIMESTAMP NOT NULL,
  target_metric VARCHAR(40) NOT NULL,
  department_id VARCHAR(20) REFERENCES departments(department_id),
  forecast_for TIMESTAMP NOT NULL,
  horizon_hours INTEGER NOT NULL CHECK (horizon_hours > 0),
  predicted_value NUMERIC(12,3) NOT NULL,
  lower_bound NUMERIC(12,3),
  upper_bound NUMERIC(12,3),
  model_name VARCHAR(60) NOT NULL,
  model_version VARCHAR(40) NOT NULL,
  actual_value NUMERIC(12,3),
  error_value NUMERIC(12,3)
);

CREATE TABLE simulation_runs (
  simulation_id VARCHAR(30) PRIMARY KEY,
  created_at TIMESTAMP NOT NULL,
  scenario_name VARCHAR(100) NOT NULL,
  scenario_type VARCHAR(40) NOT NULL,
  baseline_or_scenario VARCHAR(20) NOT NULL CHECK (baseline_or_scenario IN ('BASELINE','WHAT_IF')),
  random_seed INTEGER NOT NULL,
  start_time TIMESTAMP NOT NULL,
  end_time TIMESTAMP NOT NULL,
  status VARCHAR(20) NOT NULL CHECK (status IN ('QUEUED','RUNNING','COMPLETED','FAILED')),
  parameters_json TEXT NOT NULL,
  results_json TEXT,
  notes TEXT
);

CREATE TABLE simulation_events (
  simulation_event_id VARCHAR(30) PRIMARY KEY,
  simulation_id VARCHAR(30) NOT NULL REFERENCES simulation_runs(simulation_id),
  event_time TIMESTAMP NOT NULL,
  entity_type VARCHAR(30) NOT NULL,
  entity_id VARCHAR(30) NOT NULL,
  event_type VARCHAR(40) NOT NULL,
  department_id VARCHAR(20) REFERENCES departments(department_id),
  value_numeric NUMERIC(12,3),
  details_json TEXT
);

CREATE TABLE decisions (
  decision_id VARCHAR(30) PRIMARY KEY,
  created_at TIMESTAMP NOT NULL,
  simulation_id VARCHAR(30) REFERENCES simulation_runs(simulation_id),
  bottleneck VARCHAR(100) NOT NULL,
  recommendation TEXT NOT NULL,
  expected_wait_change_pct NUMERIC(8,2),
  expected_occupancy_change_pct NUMERIC(8,2),
  expected_cost_change_egp NUMERIC(14,2),
  expected_revenue_risk_change_egp NUMERIC(14,2),
  confidence_score NUMERIC(5,4),
  assumptions_json TEXT NOT NULL,
  approval_status VARCHAR(20) NOT NULL CHECK (approval_status IN ('PENDING','APPROVED','REJECTED','ROLLED_BACK')),
  approved_by_role VARCHAR(40),
  approved_at TIMESTAMP,
  audit_notes TEXT
);

CREATE TABLE policy_documents (
  document_id VARCHAR(30) PRIMARY KEY,
  title VARCHAR(200) NOT NULL,
  policy_type VARCHAR(40) NOT NULL,
  department_id VARCHAR(20) REFERENCES departments(department_id),
  version VARCHAR(30) NOT NULL,
  effective_from DATE NOT NULL,
  effective_to DATE,
  content TEXT NOT NULL,
  approved BOOLEAN NOT NULL DEFAULT FALSE,
  source_label VARCHAR(100) NOT NULL
);
