import { grantLabel, grantState } from '@/api/grants'
import type { GrantInfo } from '@/api/types'
import { Button } from '@/components/ui/button'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { Mono, Pill } from '../ticket/shared'
import { fmtClock, fmtDay, fmtSpan } from '@/lib/time'
import { foldedColumns } from '@/lib/columnFold'
import { useElementWidth } from '@/lib/useElementWidth'


const TONE = { active: 'brand', expired: 'neutral', revoked: 'danger' } as const
const hm = (iso: string) => fmtClock(iso)
/** Today's times are a time of day; other days carry their date ("8 Oct 17:00"). */
const day = (iso: string, now: number) => (iso.slice(0, 10) === new Date(now).toISOString().slice(0, 10) ? '' : `${fmtDay(iso)} `)

function countdown(until: string, now: number) {
  return `${fmtSpan(Date.parse(until) - now)} left`
}

export function Grants({ grants, now, name, canRevoke, onRevoke }: { grants: GrantInfo[]; now: string; name: (id: string) => string; canRevoke: (g: GrantInfo) => boolean; onRevoke: (g: GrantInfo) => void }) {
  const at = Date.parse(now)
  // A narrow page area (N11): Person, Issued and Sessions move under the grant (core column rule).
  const [frame, width] = useElementWidth<HTMLDivElement>()
  const folded = new Set(foldedColumns(GRANT_COLUMNS, width, { actions: true }))
  const show = (k: string) => !folded.has(k)
  return (
    <div ref={frame}>
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead>Grant</TableHead>
            {show('person') && <TableHead>Person</TableHead>}
            <TableHead>Scope</TableHead>
            {show('issued') && <TableHead>Issued</TableHead>}
            <TableHead>Until</TableHead>
            {show('sessions') && <TableHead>Sessions</TableHead>}
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
                  {folded.size > 0 && (
                    <span className="mt-0.5 flex flex-wrap gap-x-3 text-[12px] text-text-muted" data-fold-line>
                      {folded.has('person') && (
                        <span>
                          <span className="text-text-faint">Person</span> {name(g.person)}
                        </span>
                      )}
                      {folded.has('issued') && (
                        <span className="tabular-nums">
                          <span className="text-text-faint">Issued</span> {day(g.issued_at, at)}
                          {hm(g.issued_at)}
                        </span>
                      )}
                      {folded.has('sessions') && (
                        <span className="tabular-nums">
                          <span className="text-text-faint">Sessions</span> {g.sessions.length}
                        </span>
                      )}
                    </span>
                  )}
                </TableCell>
                {show('person') && <TableCell>{name(g.person)}</TableCell>}
                <TableCell>
                  <Mono>{g.scope}</Mono>
                </TableCell>
                {show('issued') && (
                  <TableCell className="tabular-nums text-text-muted">
                    {day(g.issued_at, at)}
                    {hm(g.issued_at)}
                  </TableCell>
                )}
                <TableCell className="whitespace-normal tabular-nums">
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
                {show('sessions') && <TableCell className="tabular-nums">{g.sessions.length}</TableCell>}
                <TableCell>
                  <Pill tone={TONE[state]} className="capitalize">{state}</Pill>
                </TableCell>
                <TableCell className="text-right">
                  {state === 'active' && canRevoke(g) && (
                    <Button variant="outline" size="xs" className="text-danger hover:text-danger" onClick={() => onRevoke(g)}>
                      Revoke
                    </Button>
                  )}
                </TableCell>
              </TableRow>
            )
          })}
        </TableBody>
      </Table>
    </div>
  )
}

const GRANT_COLUMNS = [
  { key: 'grant' },
  { key: 'person', hideBelow: 900 },
  { key: 'scope', keep: true },
  { key: 'issued', hideBelow: 900 },
  { key: 'until', keep: true },
  { key: 'sessions', hideBelow: 900 },
  { key: 'state', keep: true },
]
