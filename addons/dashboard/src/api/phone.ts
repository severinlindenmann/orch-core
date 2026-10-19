export interface PhoneRequest { ref: string; gate: 'requirements' | 'plan'; gate_gen: number }
export interface PhoneHint extends PhoneRequest {
  id: string
  uid: string
  created_at: string
  expires_at: string
  state: 'open' | 'done' | 'stale' | 'expired' | 'cancelled'
  outcome?: { kind: 'approved' | 'changes_requested'; seq: number; event_id: string; at: string }
}
