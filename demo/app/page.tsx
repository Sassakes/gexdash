"use client"

import React, { useRef, useEffect, useState, useCallback } from "react"
import { IntroAnimation } from "@/components/intro-animation"
import { RevealText } from "@/components/reveal-text"
import { MobileNav } from "@/components/mobile-nav"
import { GexHeroBackground } from "@/components/gex-hero-background"

const DASHBOARD_URL = "https://www.dash.gexdash.app"
const LIBRARY_URL = "https://thehub.gexdash.app"

// ─── Intersection Observer hook ──────────────────────────────────────────────
function useInView(threshold = 0.15) {
  const ref = useRef<HTMLDivElement>(null)
  const [inView, setInView] = useState(false)
  useEffect(() => {
    const el = ref.current
    if (!el) return
    const obs = new IntersectionObserver(([e]) => { if (e.isIntersecting) setInView(true) }, { threshold })
    obs.observe(el)
    return () => obs.disconnect()
  }, [threshold])
  return { ref, inView }
}

// ─── Dark panel card with cursor glow ─────────────────────────────────────────
function Panel({ children, className = "", delay = 0 }: { children: React.ReactNode; className?: string; delay?: number }) {
  const { ref, inView } = useInView(0.1)
  return (
    <div
      ref={ref}
      className={`group relative rounded-2xl border border-white/[0.08] bg-[#0c1119] overflow-hidden transition-colors duration-300 hover:border-white/[0.18] ${className}`}
      style={{
        opacity: inView ? 1 : 0,
        transform: inView ? "translateY(0)" : "translateY(28px)",
        transition: `opacity 0.7s ease ${delay}ms, transform 0.7s ease ${delay}ms, border-color 0.3s ease`,
      }}
    >
      <div className="pointer-events-none absolute inset-0 opacity-0 group-hover:opacity-100 transition-opacity duration-500"
        style={{ background: "radial-gradient(420px circle at var(--mouse-x, 50%) var(--mouse-y, 50%), rgba(95,227,176,0.06), transparent 60%)" }}
      />
      {children}
    </div>
  )
}

// ─── Mono eyebrow tag ──────────────────────────────────────────────────────────
function Tag({ children }: { children: React.ReactNode }) {
  return (
    <span className="inline-flex items-center gap-1.5 px-3 py-1 rounded-full font-mono text-[11px] tracking-widest text-white/50 bg-white/[0.04] border border-white/[0.08]">
      {children}
    </span>
  )
}

// ─── Section header ─────────────────────────────────────────────────────────────
function SectionHead({ tag, title, desc, right }: { tag: string; title: string; desc?: string; right?: React.ReactNode }) {
  return (
    <div className="flex flex-col md:flex-row md:items-end md:justify-between gap-8 mb-14">
      <div>
        <Tag>{tag}</Tag>
        <RevealText className="mt-5 text-4xl md:text-5xl font-light tracking-tight leading-[1.05] text-white" style={{ fontFamily: '"IBM Plex Sans", sans-serif' }}>
          {title}
        </RevealText>
      </div>
      {desc && <p className="text-sm text-white/45 leading-relaxed max-w-xs">{desc}</p>}
      {right}
    </div>
  )
}

const ACCENT = { green: "#5fe3b0", red: "#f2607d", cyan: "#5cc8ff", amber: "#f5c86b" }

// ─── Markets data (placeholder — swap with live feed later) ─────────────────────
const MARKETS = [
  { sym: "NQ",   name: "Nasdaq 100 Futures", px: "20,418.25", chg: "+0.62%", up: true,  call: "20,600", flip: "20,350", put: "20,050" },
  { sym: "ES",   name: "S&P 500 Futures",    px: "5,742.50",  chg: "+0.38%", up: true,  call: "5,800",  flip: "5,720",  put: "5,650"  },
  { sym: "SPX",  name: "S&P 500 Index",      px: "5,738.17",  chg: "-0.11%", up: false, call: "5,800",  flip: "5,725",  put: "5,650"  },
  { sym: "GOLD", name: "Gold Futures",       px: "2,668.40",  chg: "+0.94%", up: true,  call: "2,700",  flip: "2,655",  put: "2,600"  },
]

// ─── The Hub library categories (from thehub.gexdash.app) ──────────────
const LIBRARY = [
  { cat: "FOOT PRINT", accent: ACCENT.green, items: ["Breakout Mastery", "Footprint Execution ICT", "Breakout Mastery 2"] },
  { cat: "ORDER FLOW", accent: ACCENT.cyan,  items: ["Orderflow Fondamentaux", "Orderflow Open US"] },
  { cat: "ICT",        accent: ACCENT.amber, items: ["Gb Time Lexique", "Rejection Blocks", "PO3 AMD", "FVG · IFVG Masterclass", "Gb Time Presentation"] },
  { cat: "OPTIONS / GEX", accent: ACCENT.red, items: ["Gex Gamma Notes", "GAMMA HUB"] },
]

const INDICATORS = [
  { name: "CVD Divergence Scalper", meta: "NQ / ES / RTY · Pine Script" },
  { name: "EMA Hub — TF Lock",      meta: "Multi-timeframe · Pine Script" },
  { name: "Volumized Fair Value Gaps MTF", meta: "FVG · Pine Script" },
]

// ─── Main page ────────────────────────────────────────────────────────────────
export default function TheHubPage() {
  const [email, setEmail] = useState("")
  const [submitted, setSubmitted] = useState(false)
  const [copied, setCopied] = useState(false)
  const [heroReady, setHeroReady] = useState(false)
  const handleIntroDone = useCallback(() => { setHeroReady(true) }, [])

  const handleMouse = (e: React.MouseEvent<HTMLDivElement>) => {
    const el = e.currentTarget
    const rect = el.getBoundingClientRect()
    el.style.setProperty("--mouse-x", `${e.clientX - rect.left}px`)
    el.style.setProperty("--mouse-y", `${e.clientY - rect.top}px`)
  }

  const handleCopy = () => {
    navigator.clipboard?.writeText("GEX_DAILY_LEVELS // NQ ES SPX GOLD — call/put walls + gamma flip").catch(() => {})
    setCopied(true)
    setTimeout(() => setCopied(false), 1800)
  }

  return (
    <div className="bg-[#080b11] text-white min-h-screen font-sans antialiased">

      {/* ── INTRO ANIMATION ───────────────────────────────────────────────── */}
      <IntroAnimation onDone={handleIntroDone} />

      {/* ── STICKY NAV ────────────────────────────────────────────────────── */}
      <MobileNav />

      {/* ── HERO ──────────────────────────────────────────────────────────── */}
      <section className="relative min-h-screen overflow-hidden bg-[#080b11] text-white">

        {/* Financial-terminal backdrop */}
        <GexHeroBackground />

        {/* Legibility gradients */}
        <div className="absolute inset-0 z-10 pointer-events-none" style={{ background: "linear-gradient(90deg, rgba(8,11,17,0.94) 0%, rgba(8,11,17,0.82) 34%, rgba(8,11,17,0.45) 62%, rgba(8,11,17,0.15) 100%)" }} />
        <div className="absolute inset-x-0 bottom-0 z-10 pointer-events-none" style={{ height: "20%", background: "linear-gradient(to top, #080b11 0%, rgba(8,11,17,0.5) 55%, transparent 100%)" }} />

        {/* Ticker tape */}
        <div
          className="absolute inset-x-0 top-24 z-20 border-y border-white/10 bg-white/[0.02] backdrop-blur-sm"
          style={{ opacity: heroReady ? 1 : 0, transition: "opacity 0.8s ease 200ms" }}
        >
          <div className="flex items-center gap-8 overflow-x-auto px-6 md:px-12 py-2.5 font-mono text-[12px] tracking-wide no-scrollbar">
            {MARKETS.map((t) => (
              <div key={t.sym} className="flex items-center gap-2 whitespace-nowrap">
                <span className="text-white/50">{t.sym}</span>
                <span className="text-white/90">{t.px}</span>
                <span style={{ color: t.up ? ACCENT.green : ACCENT.red }}>{t.chg}</span>
              </div>
            ))}
            <div className="flex items-center gap-2 whitespace-nowrap">
              <span className="h-1.5 w-1.5 rounded-full animate-pulse" style={{ background: ACCENT.green }} />
              <span className="text-white/50">LIVE · GEX 1m</span>
            </div>
          </div>
        </div>

        {/* Hero copy */}
        <div className="relative z-30 mx-auto flex min-h-screen max-w-6xl flex-col justify-center px-6 md:px-12 pt-40 pb-24">
          <div className="max-w-2xl">
            <div
              className="mb-6 inline-flex items-center gap-2 rounded-full border border-white/15 bg-white/[0.04] px-3 py-1 font-mono text-[11px] tracking-widest text-white/60"
              style={{ opacity: heroReady ? 1 : 0, transform: heroReady ? "translateY(0)" : "translateY(12px)", transition: "opacity 0.7s ease, transform 0.7s ease" }}
            >
              THEHUB · GEXDASHBOARD
            </div>

            <h1
              className="text-5xl sm:text-6xl md:text-7xl font-light leading-[1.02] tracking-tight text-balance"
              style={{
                fontFamily: '"IBM Plex Sans", sans-serif',
                opacity: heroReady ? 1 : 0,
                filter: heroReady ? "blur(0px)" : "blur(14px)",
                transform: heroReady ? "translateY(0px)" : "translateY(28px)",
                transition: "opacity 1s cubic-bezier(0.16,1,0.3,1), filter 1s cubic-bezier(0.16,1,0.3,1), transform 1s cubic-bezier(0.16,1,0.3,1)",
              }}
            >
              Read the gamma.<br />
              <span style={{ color: ACCENT.green }}>Trade the levels.</span>
            </h1>

            <p
              className="mt-6 max-w-xl text-base sm:text-lg leading-relaxed text-white/60"
              style={{ opacity: heroReady ? 1 : 0, transform: heroReady ? "translateY(0)" : "translateY(16px)", transition: "opacity 0.9s ease 150ms, transform 0.9s ease 150ms" }}
            >
              Live gamma-exposure mapping across NQ, ES, SPX and Gold — call walls, put walls
              and the daily gamma flip, refreshed every minute. Plus dark-pool prints and a
              ready-to-paste Pine indicator for TradingView.
            </p>

            <div
              className="mt-9 flex flex-wrap items-center gap-3"
              style={{ opacity: heroReady ? 1 : 0, transform: heroReady ? "translateY(0)" : "translateY(16px)", transition: "opacity 0.9s ease 260ms, transform 0.9s ease 260ms" }}
            >
              <a href={DASHBOARD_URL} target="_blank" rel="noopener noreferrer"
                className="group inline-flex items-center gap-2 rounded-xl px-6 py-3 text-sm font-medium text-[#062018] transition-transform duration-200 hover:scale-[1.02]"
                style={{ background: ACCENT.green }}>
                Open the dashboard
                <span className="transition-transform duration-200 group-hover:translate-x-0.5">→</span>
              </a>
              <a href={LIBRARY_URL} target="_blank" rel="noopener noreferrer"
                className="inline-flex items-center gap-2 rounded-xl border border-white/15 px-6 py-3 text-sm text-white/70 transition-colors duration-200 hover:border-white/30 hover:text-white">
                Browse The Hub library
              </a>
            </div>

            <div
              className="mt-14 flex flex-wrap gap-8 sm:gap-12"
              style={{ opacity: heroReady ? 1 : 0, transform: heroReady ? "translateY(0)" : "translateY(16px)", transition: "opacity 0.9s ease 380ms, transform 0.9s ease 380ms" }}
            >
              {[
                { value: "4", label: "Indices tracked" },
                { value: "1m", label: "Refresh rate" },
                { value: "GEX", label: "Levels · EM · Flip" },
              ].map((stat) => (
                <div key={stat.label}>
                  <div className="text-3xl sm:text-4xl font-light tracking-tight text-white" style={{ fontFamily: '"IBM Plex Sans", sans-serif' }}>{stat.value}</div>
                  <div className="mt-1 font-mono text-[11px] uppercase tracking-widest text-white/40">{stat.label}</div>
                </div>
              ))}
            </div>
          </div>
        </div>
      </section>

      {/* ── MODULES (bento) ────────────────────────────────────────────────── */}
      <section id="modules" className="py-28 px-6 md:px-12 lg:px-20 border-t border-white/[0.06]">
        <div className="max-w-6xl mx-auto">
          <SectionHead
            tag="MODULES"
            title={"One screen for the\nwhole gamma picture."}
            desc="Everything the desk needs in a single terminal — levels, flow, expected move and a shareable Pine string."
          />

          <div className="grid grid-cols-12 gap-3" onMouseMove={handleMouse}>
            {/* Big card — live GEX chart */}
            <Panel className="col-span-12 lg:col-span-8 min-h-[360px] flex flex-col" delay={0}>
              <div className="relative flex-1 min-h-[280px]">
                <GexHeroBackground />
                <div className="absolute inset-x-0 bottom-0 h-24 pointer-events-none" style={{ background: "linear-gradient(to top, #0c1119 0%, transparent 100%)" }} />
                <div className="absolute left-5 top-5 flex items-center gap-2">
                  {["1m", "5m", "15m", "1D"].map((tf, i) => (
                    <span key={tf} className={`font-mono text-[11px] px-2.5 py-1 rounded-md border ${i === 0 ? "border-white/25 text-white bg-white/[0.06]" : "border-white/10 text-white/40"}`}>{tf}</span>
                  ))}
                </div>
              </div>
              <div className="relative z-10 p-6 border-t border-white/[0.06]">
                <div className="flex items-center gap-2 mb-2">
                  <span className="h-1.5 w-1.5 rounded-full animate-pulse" style={{ background: ACCENT.green }} />
                  <span className="font-mono text-[11px] tracking-widest text-white/40">GEX LEVELS · LIVE CHART</span>
                </div>
                <h3 className="text-xl font-light mb-2">Levels superimposed on price</h3>
                <p className="text-sm text-white/45 leading-relaxed max-w-lg">
                  Call wall, put wall and the daily gamma flip drawn straight onto the chart, so you always know where dealers hedge and where price is likely to stall or accelerate.
                </p>
              </div>
            </Panel>

            {/* Right column stack */}
            <div className="col-span-12 lg:col-span-4 grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-1 gap-3">
              {/* Dark pool */}
              <Panel className="p-6 min-h-[172px] flex flex-col justify-between" delay={100}>
                <div>
                  <div className="flex items-center justify-between mb-4">
                    <span className="font-mono text-[11px] tracking-widest text-white/40">DARK POOL</span>
                    <span className="font-mono text-[10px] text-white/30">FINRA · J-1</span>
                  </div>
                  <h3 className="text-lg font-light mb-1">Off-exchange prints</h3>
                  <p className="text-xs text-white/40 leading-relaxed">Where the size really traded, published daily and mapped against your levels.</p>
                </div>
                <div className="mt-4 flex items-end gap-1.5 h-8">
                  {[40, 62, 30, 78, 52, 88, 46, 70, 58].map((h, i) => (
                    <span key={i} className="flex-1 rounded-sm" style={{ height: `${h}%`, background: i === 5 ? ACCENT.cyan : "rgba(92,200,255,0.28)" }} />
                  ))}
                </div>
              </Panel>

              {/* Expected move */}
              <Panel className="p-6 min-h-[172px] flex flex-col justify-between" delay={160}>
                <div className="flex items-center justify-between mb-4">
                  <span className="font-mono text-[11px] tracking-widest text-white/40">EXPECTED MOVE</span>
                  <span className="font-mono text-[10px] text-white/30">GEX EM · 1D</span>
                </div>
                <div>
                  <div className="flex items-baseline gap-2">
                    <span className="text-3xl font-light" style={{ fontFamily: '"IBM Plex Sans", sans-serif' }}>±1.2%</span>
                    <span className="font-mono text-[11px]" style={{ color: ACCENT.green }}>open EM</span>
                  </div>
                  <p className="mt-2 text-xs text-white/40 leading-relaxed">The implied daily range from open, updated live through the session.</p>
                </div>
              </Panel>
            </div>

            {/* String Pine — full width */}
            <Panel className="col-span-12 p-6 md:p-8 flex flex-col md:flex-row md:items-center gap-6 md:gap-10" delay={220}>
              <div className="md:max-w-sm">
                <span className="font-mono text-[11px] tracking-widest text-white/40">STRING PINE</span>
                <h3 className="mt-3 text-xl font-light mb-2">GEX Daily Levels for TradingView</h3>
                <p className="text-sm text-white/45 leading-relaxed">
                  One click copies the day&apos;s levels as a Pine string. Paste it into the indicator and your chart is annotated in seconds.
                </p>
                <button
                  onClick={handleCopy}
                  className="mt-5 inline-flex items-center gap-2 rounded-xl px-5 py-2.5 text-sm font-medium text-[#062018] transition-transform duration-200 hover:scale-[1.02]"
                  style={{ background: ACCENT.green }}
                >
                  {copied ? "Copied ✓" : "Copy the Pine string"}
                </button>
              </div>
              <div className="flex-1 w-full rounded-xl border border-white/[0.08] bg-[#070a10] p-4 font-mono text-[12px] leading-relaxed overflow-x-auto no-scrollbar">
                <span className="text-white/25">// GEX Daily Levels — auto-generated</span><br />
                <span style={{ color: ACCENT.cyan }}>callWall</span> <span className="text-white/40">=</span> <span style={{ color: ACCENT.green }}>20600</span><br />
                <span style={{ color: ACCENT.cyan }}>gammaFlip</span> <span className="text-white/40">=</span> <span className="text-white/70">20350</span><br />
                <span style={{ color: ACCENT.cyan }}>putWall</span> <span className="text-white/40">=</span> <span style={{ color: ACCENT.red }}>20050</span><br />
                <span style={{ color: ACCENT.cyan }}>expMove</span> <span className="text-white/40">=</span> <span className="text-white/70">0.012</span>
              </div>
            </Panel>
          </div>
        </div>
      </section>

      {/* ── MARKETS ────────────────────────────────────────────────────────── */}
      <section id="markets" className="py-28 px-6 md:px-12 lg:px-20 border-t border-white/[0.06]">
        <div className="max-w-6xl mx-auto">
          <SectionHead
            tag="MARKETS"
            title={"Four markets,\none methodology."}
            desc="The same gamma read-out on the instruments that matter — index futures, the cash index and gold."
          />

          <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-3" onMouseMove={handleMouse}>
            {MARKETS.map((m, i) => (
              <Panel key={m.sym} className="p-6" delay={i * 80}>
                <div className="flex items-center justify-between">
                  <span className="text-lg font-medium tracking-tight" style={{ fontFamily: '"IBM Plex Sans", sans-serif' }}>{m.sym}</span>
                  <span className="font-mono text-[12px]" style={{ color: m.up ? ACCENT.green : ACCENT.red }}>{m.chg}</span>
                </div>
                <div className="mt-1 font-mono text-[11px] text-white/35">{m.name}</div>
                <div className="mt-4 text-2xl font-light" style={{ fontFamily: '"IBM Plex Sans", sans-serif' }}>{m.px}</div>

                <div className="mt-5 space-y-2 font-mono text-[11px]">
                  <div className="flex items-center justify-between">
                    <span className="text-white/40">Call wall</span>
                    <span style={{ color: ACCENT.green }}>{m.call}</span>
                  </div>
                  <div className="flex items-center justify-between">
                    <span className="text-white/40">Gamma flip</span>
                    <span style={{ color: ACCENT.cyan }}>{m.flip}</span>
                  </div>
                  <div className="flex items-center justify-between">
                    <span className="text-white/40">Put wall</span>
                    <span style={{ color: ACCENT.red }}>{m.put}</span>
                  </div>
                </div>
              </Panel>
            ))}
          </div>
        </div>
      </section>

      {/* ── WORKFLOW ───────────────────────────────────────────────────────── */}
      <section id="workflow" className="py-28 px-6 md:px-12 lg:px-20 border-t border-white/[0.06]">
        <div className="max-w-6xl mx-auto">
          <SectionHead
            tag="WORKFLOW"
            title={"From open to close\nin four moves."}
            desc="A repeatable pre-market routine that takes about two minutes."
          />

          <div className="grid grid-cols-1 md:grid-cols-4 gap-3" onMouseMove={handleMouse}>
            {[
              { n: "01", title: "Pick your index", desc: "Select NQ, ES, SPX or Gold. Levels load instantly for the current session." },
              { n: "02", title: "Read the walls",  desc: "Note the call wall, put wall and gamma flip — your map of support and resistance." },
              { n: "03", title: "Copy the Pine",   desc: "Grab the daily string and paste it into the TradingView indicator." },
              { n: "04", title: "Trade the session", desc: "Fade the walls, respect the flip, size around the expected move." },
            ].map((step, i) => (
              <Panel key={step.n} className="p-7 flex flex-col min-h-[220px]" delay={i * 80}>
                <span className="font-mono text-[11px] tracking-widest" style={{ color: ACCENT.green }}>{step.n}</span>
                <h3 className="mt-auto pt-14 text-xl font-light mb-2">{step.title}</h3>
                <p className="text-sm text-white/45 leading-relaxed">{step.desc}</p>
              </Panel>
            ))}
          </div>
        </div>
      </section>

      {/* ── LIBRARY (The Hub) ──────────────────────────────────────────────── */}
      <section id="library" className="py-28 px-6 md:px-12 lg:px-20 border-t border-white/[0.06]">
        <div className="max-w-6xl mx-auto">
          <SectionHead
            tag="THE HUB · LIBRARY"
            title={"A free library\nbehind the dashboard."}
            right={
              <a href={LIBRARY_URL} target="_blank" rel="noopener noreferrer"
                className="inline-flex items-center gap-2 rounded-xl border border-white/15 px-5 py-2.5 text-sm text-white/70 transition-colors duration-200 hover:border-white/30 hover:text-white self-start md:self-end">
                Open the library →
              </a>
            }
          />

          <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-3 mb-3" onMouseMove={handleMouse}>
            {LIBRARY.map((c, i) => (
              <Panel key={c.cat} className="p-6 flex flex-col" delay={i * 70}>
                <div className="flex items-center justify-between mb-4">
                  <span className="font-mono text-[11px] tracking-widest" style={{ color: c.accent }}>{c.cat}</span>
                  <span className="font-mono text-[11px] text-white/30">{c.items.length} files</span>
                </div>
                <ul className="space-y-2.5 flex-1">
                  {c.items.map((it) => (
                    <li key={it} className="flex items-start gap-2.5 text-sm text-white/60">
                      <span className="mt-1.5 h-1 w-1 rounded-full shrink-0" style={{ background: c.accent }} />
                      {it}
                    </li>
                  ))}
                </ul>
              </Panel>
            ))}
          </div>

          {/* TradingView indicators */}
          <Panel className="p-6 md:p-8" delay={120}>
            <div className="flex items-center justify-between mb-6">
              <div>
                <span className="font-mono text-[11px] tracking-widest text-white/40">TRADINGVIEW INDICATORS</span>
                <h3 className="mt-2 text-lg font-light">Published Pine scripts</h3>
              </div>
              <span className="font-mono text-[11px] text-white/30 hidden sm:block">open source</span>
            </div>
            <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
              {INDICATORS.map((ind) => (
                <div key={ind.name} className="rounded-xl border border-white/[0.08] bg-[#070a10] p-5 hover:border-white/[0.16] transition-colors">
                  <div className="flex items-start justify-between gap-3">
                    <h4 className="text-sm font-light leading-snug">{ind.name}</h4>
                    <span className="text-white/25 text-xs shrink-0">↗</span>
                  </div>
                  <p className="mt-2 font-mono text-[11px] text-white/35">{ind.meta}</p>
                </div>
              ))}
            </div>
          </Panel>
        </div>
      </section>

      {/* ── CAPABILITIES MARQUEE ───────────────────────────────────────────── */}
      <section className="py-0 border-t border-white/[0.06] overflow-hidden select-none">
        <div className="flex border-b border-white/[0.06]" style={{ animation: "marqueeLeft 30s linear infinite" }}>
          {[...Array(3)].map((_, rep) => (
            <div key={rep} className="flex shrink-0">
              {["Call Walls", "Put Walls", "Gamma Flip", "Expected Move", "Dark Pool", "0DTE Flow", "Vanna", "Charm", "Net GEX", "Dealer Hedging"].map((cap) => (
                <div key={cap} className="flex items-center gap-6 px-10 py-5 border-r border-white/[0.06] shrink-0">
                  <span className="w-1.5 h-1.5 rounded-full shrink-0" style={{ background: "rgba(95,227,176,0.5)" }} />
                  <span className="text-sm text-white/45 whitespace-nowrap tracking-wide">{cap}</span>
                </div>
              ))}
            </div>
          ))}
        </div>
        <div className="flex" style={{ animation: "marqueeRight 24s linear infinite" }}>
          {[...Array(3)].map((_, rep) => (
            <div key={rep} className="flex shrink-0">
              {["Order Flow", "Footprint", "ICT Concepts", "FVG", "PO3 · AMD", "Rejection Blocks", "CVD Divergence", "Breakout Mastery", "TradingView Pine", "Session Levels"].map((cap) => (
                <div key={cap} className="flex items-center gap-6 px-10 py-5 border-r border-white/[0.06] shrink-0">
                  <span className="w-1.5 h-1.5 rounded-full shrink-0" style={{ background: "rgba(255,255,255,0.15)" }} />
                  <span className="text-sm text-white/30 whitespace-nowrap tracking-wide">{cap}</span>
                </div>
              ))}
            </div>
          ))}
        </div>
      </section>

      {/* ── ACCESS / CTA ───────────────────────────────────────────────────── */}
      <section id="access" className="relative py-28 px-6 md:px-12 lg:px-20 border-t border-white/[0.06] overflow-hidden">
        {/* subtle glow */}
        <div className="pointer-events-none absolute inset-0" style={{ background: "radial-gradient(600px circle at 50% 0%, rgba(95,227,176,0.08), transparent 65%)" }} />
        <div className="relative z-10 max-w-2xl mx-auto text-center">
          <div className="inline-flex items-center gap-2 rounded-full border border-white/15 bg-white/[0.04] px-3 py-1 font-mono text-[11px] tracking-widest text-white/60 mb-6">
            <span className="h-1.5 w-1.5 rounded-full animate-pulse" style={{ background: ACCENT.amber }} />
            DISCORD · COMING SOON
          </div>
          <h2 className="text-4xl md:text-5xl lg:text-6xl font-light tracking-tight leading-[1.05] mb-6 text-balance" style={{ fontFamily: '"IBM Plex Sans", sans-serif' }}>
            Get on the gamma edge.
          </h2>
          <p className="text-sm text-white/45 leading-relaxed mb-10 max-w-md mx-auto">
            The Hub Discord is in the works. Drop your email and we&apos;ll let you know the moment it opens — plus dashboard updates.
          </p>
          {!submitted ? (
            <form
              onSubmit={e => { e.preventDefault(); if (email) setSubmitted(true) }}
              className="flex flex-col sm:flex-row gap-2 max-w-md mx-auto"
            >
              <input
                type="email"
                placeholder="you@email.com"
                value={email}
                onChange={e => setEmail(e.target.value)}
                required
                className="flex-1 bg-white/[0.03] border border-white/15 rounded-xl px-4 py-3 text-sm text-white placeholder:text-white/30 focus:outline-none focus:border-white/35 transition-colors"
              />
              <button
                type="submit"
                className="px-8 py-3 text-sm rounded-xl transition-transform duration-200 hover:scale-[1.02] tracking-widest font-medium text-[#062018]"
                style={{ background: ACCENT.green }}
              >
                NOTIFY ME
              </button>
            </form>
          ) : (
            <div className="inline-flex items-center gap-2 px-6 py-3 rounded-xl border text-sm" style={{ borderColor: "rgba(95,227,176,0.3)", background: "rgba(95,227,176,0.08)", color: ACCENT.green }}>
              <span className="w-1.5 h-1.5 rounded-full" style={{ background: ACCENT.green }} />
              {"You're on the list. We'll be in touch."}
            </div>
          )}

          <div className="mt-8 flex items-center justify-center gap-3">
            <a href={DASHBOARD_URL} target="_blank" rel="noopener noreferrer"
              className="inline-flex items-center gap-2 text-sm text-white/55 hover:text-white transition-colors">
              Open the dashboard →
            </a>
          </div>
        </div>
      </section>

      {/* ── FOOTER ────────────────────────────────────────────────────────── */}
      <footer className="py-10 px-6 md:px-12 lg:px-20 border-t border-white/[0.06]">
        <div className="max-w-6xl mx-auto flex flex-col md:flex-row items-start md:items-center justify-between gap-8">
          <span className="font-pixel text-xs tracking-[0.25em] text-white/60">THEHUB</span>

          <div className="flex flex-wrap items-center gap-x-8 gap-y-3">
            {[
              { label: "Modules",  href: "#modules" },
              { label: "Markets",  href: "#markets" },
              { label: "Workflow", href: "#workflow" },
              { label: "Library",  href: "#library" },
              { label: "Access",   href: "#access" },
            ].map(l => (
              <a key={l.label} href={l.href} className="text-xs text-white/40 hover:text-white/80 transition-colors tracking-widest">{l.label}</a>
            ))}
          </div>

          <div className="flex items-center gap-6">
            <a href={DASHBOARD_URL} target="_blank" rel="noopener noreferrer" className="text-xs text-white/30 hover:text-white/60 transition-colors tracking-widest">Dashboard</a>
            <a href={LIBRARY_URL} target="_blank" rel="noopener noreferrer" className="text-xs text-white/30 hover:text-white/60 transition-colors tracking-widest">Library</a>
          </div>
        </div>
        <div className="max-w-6xl mx-auto mt-8 pt-6 border-t border-white/[0.05]">
          <span className="text-xs text-white/20">© 2026 TheHub · WealthBuilders. For educational purposes only — not financial advice.</span>
        </div>
      </footer>
    </div>
  )
}
