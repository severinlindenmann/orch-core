import { withTheme } from '@rjsf/core'
import validator from '@rjsf/validator-ajv8'
import type { ComponentProps } from 'react'
import { darkTheme } from './rjsfTheme'

const ThemedForm = withTheme(darkTheme)

type Props = Omit<ComponentProps<typeof ThemedForm>, 'validator'>

/** The rjsf form with the dark theme and validator. Loaded lazily by AddonNode so rjsf and ajv stay out of the main bundle. */
export default function AddonForm(props: Props) {
  return <ThemedForm {...props} validator={validator} />
}
