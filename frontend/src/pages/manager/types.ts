export interface ManagerItem {
  code: string
  name: string
  fund_type: string | null
  fund_company: string | null
  scale: number | null
  fund_manager: string | null
  return_1y: number | null
  return_3y: number | null
  managers: string | null
  start_date: string | null
  end_date: string | null
  tenure_text: string | null
  tenure_days: number | null
  tenure_return: number | null
  fetch_time: string | null
}

export interface TenureSegment {
  seq: number
  start_date: string | null
  end_date: string | null
  is_current: number
  managers: string
  tenure_text: string | null
  tenure_days: number | null
  tenure_return: number | null
}

export interface CoverageStats {
  total: number
  covered: number
  uncovered: number
}

export interface RunningTask {
  id: number
  task_type: string
  status: string
  total_count: number
  current_count: number
  success_count: number
  fail_count: number
}
