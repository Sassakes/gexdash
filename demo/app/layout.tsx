import React from "react"
import type { Metadata } from 'next'
import { Geist, Geist_Mono, IBM_Plex_Sans } from 'next/font/google'
import { Courier_Prime } from 'next/font/google'
import { Analytics } from '@vercel/analytics/next'
import './globals.css'

const _geist = Geist({ subsets: ["latin"] });
const _geistMono = Geist_Mono({ subsets: ["latin"] });
const _courierPrime = Courier_Prime({ weight: ["400", "700"], subsets: ["latin"] });
const _ibmPlexSans = IBM_Plex_Sans({ weight: ["300", "400", "500", "600"], subsets: ["latin"] });

export const metadata: Metadata = {
  title: 'TheHub — GexDashboard · Live Gamma Exposure Levels',
  description: 'Live gamma-exposure mapping across NQ, ES, SPX and Gold — call walls, put walls and the daily gamma flip, refreshed every minute. Dark-pool prints, expected move and a ready-to-paste TradingView Pine indicator.',
  keywords: ['GEX', 'gamma exposure', 'call wall', 'put wall', 'gamma flip', 'dark pool', 'NQ', 'ES', 'SPX', 'Gold', 'TradingView Pine', 'options flow', 'trading dashboard'],
  authors: [{ name: 'TheHub' }],
  openGraph: {
    title: 'TheHub — GexDashboard · Live Gamma Exposure Levels',
    description: 'Read the gamma, trade the levels. Live GEX walls and flip across NQ, ES, SPX and Gold.',
    type: 'website',
    url: 'https://gexdash.wealthbuilders.group',
    siteName: 'TheHub',
  },
  twitter: {
    card: 'summary_large_image',
    title: 'TheHub — GexDashboard · Live Gamma Exposure Levels',
    description: 'Read the gamma, trade the levels. Live GEX walls and flip across NQ, ES, SPX and Gold.',
  },
  // Metadata icons ne suivent PAS basePath automatiquement (contrairement aux
  // assets passes par l'import Next standard) -- prefixe /demo a la main,
  // sinon ces favicons 404ent une fois servis sous ce sous-chemin.
  icons: {
    icon: [
      {
        url: '/demo/icon-light-32x32.png',
        media: '(prefers-color-scheme: light)',
      },
      {
        url: '/demo/icon-dark-32x32.png',
        media: '(prefers-color-scheme: dark)',
      },
      {
        url: '/demo/icon.svg',
        type: 'image/svg+xml',
      },
    ],
    apple: '/demo/apple-icon.png',
  },
}

export const viewport = {
  themeColor: '#080b11',
}

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode
}>) {
  return (
    <html lang="en" className="bg-[#080b11]">
      <body className={`font-sans antialiased bg-[#080b11]`}>
        {children}
        <Analytics />
      </body>
    </html>
  )
}
