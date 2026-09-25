import React from 'react'
import ReactDOM from 'react-dom/client'
import '@fontsource-variable/bricolage-grotesque'
import '@fontsource/ibm-plex-sans/400.css'
import '@fontsource/ibm-plex-sans/500.css'
import '@fontsource/ibm-plex-sans/600.css'
import '@fontsource/ibm-plex-mono/400.css'
import './styles.css'
import App from './App'

// iOS Safari applies :active (the press feedback) only once a touch listener exists.
document.addEventListener('touchstart', () => undefined, { passive: true })

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
)

