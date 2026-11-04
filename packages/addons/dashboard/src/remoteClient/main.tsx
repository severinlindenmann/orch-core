import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { App } from './App'
import { createApi, launch } from './api'
import './remoteClient.css'

void launch().then((token) => {
  createRoot(document.getElementById('root')!).render(
    <StrictMode>{token ? <App api={createApi(token)} /> : <App api={null} />}</StrictMode>,
  )
})
