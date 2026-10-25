import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { RouterProvider } from '@tanstack/react-router'
import { AppProviders } from './providers'
import { router } from './router'
import '@/styles/adminator.css'
import '@/styles/tokens.css'
import '@/styles/app.css'

const container = document.getElementById('root')
if (container === null) {
  throw new Error('#root element is missing from index.html')
}

createRoot(container).render(
  <StrictMode>
    <AppProviders>
      <RouterProvider router={router} />
    </AppProviders>
  </StrictMode>,
)
