import { createContext, useContext } from 'react'

/** Picture files of the ticket in view, by name, as `data:` URLs of bytes whose sha256 equals the digest the ticket names. */
export const ArtifactImagesContext = createContext<Readonly<Record<string, string>>>({})
export const useArtifactImages = () => useContext(ArtifactImagesContext)
