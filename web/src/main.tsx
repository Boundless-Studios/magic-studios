import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'

import { initAnalytics } from './analytics'
import { App } from './app/App'
import { initMonitoring } from './monitoring'
import './styles.css'

initMonitoring()
initAnalytics()

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <App />
  </StrictMode>,
)
