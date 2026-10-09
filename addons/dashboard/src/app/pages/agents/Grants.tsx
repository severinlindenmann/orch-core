import { grantLabel, grantState } from '@/api/grants'
import type { GrantInfo } from '@/api/types'
import { Button } from '@/components/ui/button'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { Mono, Pill } from '../ticket/shared'
import { fmtClock, fmtDay, fmtSpan } from '@/lib/time'


const TONE = { active: 'brand', expired: 'neutral', revoked: 'danger' } as const
const hm = (iso: string) => fmtClock(iso)
/** Today's times are a time of day; other days carry their date ("8 Oct 17:00"). */
const day = (iso: string, now: number) => (iso.slice(0, 10) === new Date(now).toISOString().slice(0, 10) ? '' : `${fmtDay(iso)} `)

function countdown(until: string, now: number) {
  return `${fmtSpan(Date.parse(until) - now)} left`
}

export function Grants({ grants, now, name, canRevoke, onRevoke }: { grants: GrantInfo[]; now: string; name: (id: string) => string; canRevoke: (g: GrantInfo) => boolean; onRevoke: (g: GrantInfo) => void }) {
  const at = Date.parse(now)
  return (
    <Table>
      <TableHeader>
        <TableRow>
          <TableHead>Grant</TableHead>
          <TableHead>Person</TableHead>
          <TableHead>Scope</TableHead>
          <TableHead>Issued</TableHead>
          <TableHead>Until</TableHead>
          <TableHead>Sessions</TableHead>
          <TableHead>State</TableHead>
          <TableHead className="w-24" />
        </TableRow>
      </TableHeader>
      <TableBody>
        {grants.map((g) => {
          const state = grantState(g, at)
          return (
            <TableRow key={g.id}>
              <TableCell>
                <span className="block">{grantLabel(g, name(g.person), at)}</span>
                <Mono className="text-[11px] text-text-faint">{g.id}</Mono>
              </TableCell>
              <TableCell>{name(g.person)}</TableCell>
              <TableCell>
                <Mono>{g.scope}</Mono>
              </TableCell>
              <TableCell className="tabular-nums text-text-muted">
                {day(g.issued_at, at)}
                {hm(g.issued_at)}
              </TableCell>
              <TableCell className="tabular-nums">
                {g.revoked ? (
                  <span className="text-text-muted">
                    {day(g.revoked.at, at)}
                    {hm(g.revoked.at)} by {name(g.revoked.by)}
                  </span>
                ) : (
                  <>
                    {day(g.until, at)}
                    {hm(g.until)}
                    {state === 'active' && <span className="ml-2 text-xs text-text-faint">{countdown(g.until, at)}</span>}
                  </>
                )}
              </TableCell>
              <TableCell className="tabular-nums">{g.sessions.length}</TableCell>
              <TableCell>
                <Pill tone={TONE[state]}>{state}</Pill>
              </TableCell>
              <TableCell className="text-right">
                {state === 'active' && canRevoke(g) && (
                  <Button variant="destructive" size="xs" onClick={() => onRevoke(g)}>
                    Revoke
                  </Button>
                )}
              </TableCell>
            </TableRow>
          )
        })}
      </TableBody>
    </Table>
  )
}
