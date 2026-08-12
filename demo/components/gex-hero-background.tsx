"use client"

import { useEffect, useRef } from "react"

/**
 * GexHeroBackground
 * A lightweight, canvas-based "financial terminal" backdrop:
 *  - faint grid
 *  - an animated price area chart that scrolls right → left
 *  - horizontal GEX levels superimposed (Call Wall / Gamma Flip / Put Wall)
 *
 * Performance conscious: DPR capped, single rAF loop, throttled price updates,
 * and it fully stops when the user prefers reduced motion.
 */

const COLORS = {
  bg: "#080b11",
  grid: "rgba(255,255,255,0.035)",
  line: "#5fe3b0", // price line
  areaTop: "rgba(95,227,176,0.20)",
  areaBottom: "rgba(95,227,176,0.0)",
  callWall: "#4ad4a0", // positive gamma
  putWall: "#f2607d", // negative gamma
  flip: "#5aa7ff", // gamma flip
}

type Level = { label: string; y: number; color: string; dashed?: boolean }

export function GexHeroBackground() {
  const canvasRef = useRef<HTMLCanvasElement>(null)

  useEffect(() => {
    const canvas = canvasRef.current
    if (!canvas) return
    const ctx = canvas.getContext("2d")
    if (!ctx) return

    const reduce =
      typeof window !== "undefined" &&
      window.matchMedia?.("(prefers-reduced-motion: reduce)").matches

    let width = 0
    let height = 0
    let dpr = 1

    // Price series (normalised 0..1, 0 = bottom). Filled after we know width.
    let series: number[] = []
    let seed = 0.55
    let phase = 0

    // GEX levels expressed as fractions of height (0 bottom → 1 top)
    const levels: Level[] = [
      { label: "CALL WALL", y: 0.78, color: COLORS.callWall },
      { label: "GAMMA FLIP", y: 0.52, color: COLORS.flip, dashed: true },
      { label: "PUT WALL", y: 0.24, color: COLORS.putWall },
    ]

    function nextPoint() {
      // Smooth random walk gently pulled toward the gamma-flip level (0.52)
      phase += 0.045
      const drift = Math.sin(phase) * 0.06 + Math.sin(phase * 0.37) * 0.04
      const pull = (0.52 - seed) * 0.04
      seed += pull + (Math.random() - 0.5) * 0.02
      seed = Math.max(0.12, Math.min(0.9, seed))
      return Math.max(0.1, Math.min(0.92, seed + drift))
    }

    function resize() {
      const rect = canvas!.getBoundingClientRect()
      dpr = Math.min(window.devicePixelRatio || 1, 1.5)
      width = rect.width
      height = rect.height
      canvas!.width = Math.floor(width * dpr)
      canvas!.height = Math.floor(height * dpr)
      ctx!.setTransform(dpr, 0, 0, dpr, 0, 0)

      // Keep ~1 point every 8px so the line has consistent density
      const count = Math.max(24, Math.ceil(width / 8))
      if (series.length === 0) {
        series = Array.from({ length: count }, () => nextPoint())
      } else if (series.length < count) {
        while (series.length < count) series.unshift(nextPoint())
      } else if (series.length > count) {
        series = series.slice(series.length - count)
      }
    }

    function draw() {
      const step = width / (series.length - 1)

      ctx!.clearRect(0, 0, width, height)

      // Background
      ctx!.fillStyle = COLORS.bg
      ctx!.fillRect(0, 0, width, height)

      // Grid — vertical
      ctx!.strokeStyle = COLORS.grid
      ctx!.lineWidth = 1
      const gx = 96
      for (let x = 0; x <= width; x += gx) {
        ctx!.beginPath()
        ctx!.moveTo(x + 0.5, 0)
        ctx!.lineTo(x + 0.5, height)
        ctx!.stroke()
      }
      // Grid — horizontal
      const gy = 72
      for (let y = 0; y <= height; y += gy) {
        ctx!.beginPath()
        ctx!.moveTo(0, y + 0.5)
        ctx!.lineTo(width, y + 0.5)
        ctx!.stroke()
      }

      // GEX level lines
      ctx!.font =
        '600 11px ui-monospace, SFMono-Regular, Menlo, Consolas, monospace'
      levels.forEach((lvl) => {
        const y = height - lvl.y * height
        ctx!.strokeStyle = lvl.color
        ctx!.globalAlpha = 0.55
        ctx!.lineWidth = 1
        if (lvl.dashed) ctx!.setLineDash([6, 6])
        ctx!.beginPath()
        ctx!.moveTo(0, y + 0.5)
        ctx!.lineTo(width, y + 0.5)
        ctx!.stroke()
        ctx!.setLineDash([])

        // Label chip on the right
        ctx!.globalAlpha = 1
        const text = lvl.label
        const tw = ctx!.measureText(text).width
        const padX = 8
        const chipW = tw + padX * 2
        const chipH = 18
        const chipX = width - chipW - 12
        const chipY = y - chipH / 2
        ctx!.fillStyle = "rgba(8,11,17,0.85)"
        ctx!.fillRect(chipX, chipY, chipW, chipH)
        ctx!.fillStyle = lvl.color
        ctx!.fillRect(chipX, chipY, 2, chipH)
        ctx!.fillStyle = lvl.color
        ctx!.textBaseline = "middle"
        ctx!.fillText(text, chipX + padX, y + 1)
      })
      ctx!.globalAlpha = 1

      // Price area fill
      ctx!.beginPath()
      ctx!.moveTo(0, height)
      series.forEach((v, i) => {
        const x = i * step
        const y = height - v * height
        ctx!.lineTo(x, y)
      })
      ctx!.lineTo(width, height)
      ctx!.closePath()
      const grad = ctx!.createLinearGradient(0, 0, 0, height)
      grad.addColorStop(0, COLORS.areaTop)
      grad.addColorStop(1, COLORS.areaBottom)
      ctx!.fillStyle = grad
      ctx!.fill()

      // Price line
      ctx!.beginPath()
      series.forEach((v, i) => {
        const x = i * step
        const y = height - v * height
        if (i === 0) ctx!.moveTo(x, y)
        else ctx!.lineTo(x, y)
      })
      ctx!.strokeStyle = COLORS.line
      ctx!.lineWidth = 1.6
      ctx!.shadowColor = COLORS.line
      ctx!.shadowBlur = 10
      ctx!.stroke()
      ctx!.shadowBlur = 0

      // Leading dot
      const last = series[series.length - 1]
      const lx = width
      const ly = height - last * height
      ctx!.beginPath()
      ctx!.arc(lx, ly, 3, 0, Math.PI * 2)
      ctx!.fillStyle = COLORS.line
      ctx!.fill()
    }

    resize()
    draw()

    if (reduce) {
      const onResizeStatic = () => {
        resize()
        draw()
      }
      window.addEventListener("resize", onResizeStatic)
      return () => window.removeEventListener("resize", onResizeStatic)
    }

    let raf = 0
    let acc = 0
    let lastT = performance.now()
    const stepEveryMs = 90 // advance the series ~11x/sec → smooth but cheap

    const loop = (t: number) => {
      const dt = t - lastT
      lastT = t
      acc += dt
      let changed = false
      while (acc >= stepEveryMs) {
        series.push(nextPoint())
        series.shift()
        acc -= stepEveryMs
        changed = true
      }
      if (changed) draw()
      raf = requestAnimationFrame(loop)
    }
    raf = requestAnimationFrame(loop)

    const onResize = () => resize()
    window.addEventListener("resize", onResize)

    return () => {
      cancelAnimationFrame(raf)
      window.removeEventListener("resize", onResize)
    }
  }, [])

  return (
    <canvas
      ref={canvasRef}
      className="absolute inset-0 h-full w-full"
      aria-hidden="true"
    />
  )
}
