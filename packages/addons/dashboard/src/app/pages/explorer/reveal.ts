// Code and diff text keeps its backslashes (unlike visible.tsx, which doubles them), but a character the eye cannot see
// (a direction override, a zero-width mark, a control) is shown as ⟦u+XXXX⟧: code must read as what it does.
const HIDDEN = /[\p{Cc}\p{Cf}\p{Zl}\p{Zp}\p{Default_Ignorable_Code_Point}\p{Co}\p{Cn}\p{Cs}⠀]/gu

export function reveal(s: string): string {
  return s.replace(HIDDEN, (c) => (c === '\t' ? '    ' : `⟦u+${c.codePointAt(0)!.toString(16).toUpperCase().padStart(4, '0')}⟧`))
}
