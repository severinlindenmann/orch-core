import { useQuery } from '@tanstack/react-query'
import { queries } from '@/api/queries'
import type { SignMethod } from '@/api/signing'

export function useSignMethods(ws: string | undefined) {
  const settings = useQuery({ ...queries.hostSettings(ws ?? ''), enabled: !!ws, retry: false })
  const preference = useQuery({ ...queries.signDefault(ws ?? ''), enabled: !!ws, retry: false })
  const unrestricted = settings.data?.sign_methods === null
  const allowed: SignMethod[] | undefined = unrestricted ? ['terminal', 'click', 'phone'] : settings.data?.sign_methods ?? undefined
  return { allowed, unrestricted, preference: preference.data, preferenceFailed: preference.isError, loading: !!ws && (settings.isPending || preference.isPending) }
}
