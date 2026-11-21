/**
 * **功能**
 * 认证页左栏的动态天空画布（Blue sky shader，源自 FeralUI 导出件的本仓适配）。
 *
 * **归属**
 * `features/auth` 域装饰组件：只渲染，不取数、不含文案。
 *
 * **口径**
 * 纯装饰层：根节点 aria-hidden，失败静默退化为 CSS 渐变底，不弹诊断文字。
 * 色板由调用方给（`sky.skyPaletteFor`：北京时间四时段），组件不自己读钟。
 * 布局交给宿主 CSS（`.auth-sky` 定位与渐变兜底）；组件自身不占文档流语义。
 * 噪声纹理与颗粒用固定种子伪随机生成——视觉基线截图必须逐帧可复现。
 * `prefers-reduced-motion: reduce` 时只画 START_T 单帧不跑 rAF（同源判据在 shader 外层）。
 * 不可见（Intersection/visibilitychange）与 WebGL 上下文丢失都停表；恢复后重建资源。
 * 颜色只进 GLSL uniform 浮点数组与 CSS 变量；TSX 内不写字面色（token 门会红）。
 */

import { useEffect, useLayoutEffect, useRef, useState } from 'react'
import type { SkyPalette } from './sky'
const VERTEX_SHADER = 'attribute vec2 p;void main(){gl_Position=vec4(p,0.,1.);}'
const FRAGMENT_SHADER = "precision highp float;\nuniform vec2 u_res;\nuniform float u_t;\nuniform vec3 u_main;\nuniform vec3 u_low;\nuniform vec3 u_mid;\nuniform vec3 u_high;\nuniform vec3 u_cloud;\nuniform float u_wind;   // wind speed\nuniform float u_warp;   // warp power\nuniform float u_nscale; // noise scale\nuniform sampler2D u_noise;\n\nconst float FBM_STRENGTH = 0.912;\nconst float BLUR_RADIUS = 1.2673;\nconst float ZOOM = 0.3971;\nconst float GRAIN_SCALE = 2.5;\nconst float GRAIN_STRENGTH = 0.014;\n\nvec3 burn(vec3 base, vec3 blend, float op){\n  return max(base + blend - vec3(1.0), vec3(0.0))*op + base*(1.0-op);\n}\nfloat rand2(vec2 n){ return fract(sin(dot(n, vec2(12.9898, 4.1414)))*43758.5453); }\nfloat noise2(vec2 p){\n  vec2 ip = floor(p); vec2 u = fract(p);\n  u = u*u*(3.0-2.0*u);\n  float m = mix(mix(rand2(ip), rand2(ip+vec2(1.,0.)), u.x), mix(rand2(ip+vec2(0.,1.)), rand2(ip+vec2(1.,1.)), u.x), u.y);\n  return m*m;\n}\nfloat fbm4(vec2 x){\n  float v = 0.0; float a = 0.5;\n  vec2 shift = vec2(100.0);\n  mat2 rot = mat2(cos(0.5), sin(0.5), -sin(0.5), cos(0.5));\n  for (int i = 0; i < 4; i++) { v += a*noise2(x); x = rot*x*2.0 + shift; a *= 0.5; }\n  return v;\n}\nvec4 permute4(vec4 x){ return mod((x*34.0 + 1.0)*x, 289.0); }\nvec4 tisqrt(vec4 r){ return 1.79284291400159 - 0.85373472095314*r; }\nvec3 fade3(vec3 t){ return t*t*t*(t*(t*6.0-15.0)+10.0); }\nfloat cnoise(vec3 P){\n  vec3 Pi0 = floor(P); vec3 Pi1 = Pi0 + vec3(1.0);\n  Pi0 = mod(Pi0, 289.0); Pi1 = mod(Pi1, 289.0);\n  vec3 Pf0 = fract(P); vec3 Pf1 = Pf0 - vec3(1.0);\n  vec4 ix = vec4(Pi0.x, Pi1.x, Pi0.x, Pi1.x);\n  vec4 iy = vec4(Pi0.yy, Pi1.yy);\n  vec4 iz0 = vec4(Pi0.z); vec4 iz1 = vec4(Pi1.z);\n  vec4 ixy = permute4(permute4(ix) + iy);\n  vec4 ixy0 = permute4(ixy + iz0); vec4 ixy1 = permute4(ixy + iz1);\n  vec4 gx0 = ixy0/7.0; vec4 gy0 = fract(floor(gx0)/7.0) - 0.5; gx0 = fract(gx0);\n  vec4 gz0 = vec4(0.5) - abs(gx0) - abs(gy0); vec4 sz0 = step(gz0, vec4(0.0));\n  gx0 -= sz0*(step(vec4(0.0), gx0) - 0.5); gy0 -= sz0*(step(vec4(0.0), gy0) - 0.5);\n  vec4 gx1 = ixy1/7.0; vec4 gy1 = fract(floor(gx1)/7.0) - 0.5; gx1 = fract(gx1);\n  vec4 gz1 = vec4(0.5) - abs(gx1) - abs(gy1); vec4 sz1 = step(gz1, vec4(0.0));\n  gx1 -= sz1*(step(vec4(0.0), gx1) - 0.5); gy1 -= sz1*(step(vec4(0.0), gy1) - 0.5);\n  vec3 g000 = vec3(gx0.x, gy0.x, gz0.x); vec3 g100 = vec3(gx0.y, gy0.y, gz0.y);\n  vec3 g010 = vec3(gx0.z, gy0.z, gz0.z); vec3 g110 = vec3(gx0.w, gy0.w, gz0.w);\n  vec3 g001 = vec3(gx1.x, gy1.x, gz1.x); vec3 g101 = vec3(gx1.y, gy1.y, gz1.y);\n  vec3 g011 = vec3(gx1.z, gy1.z, gz1.z); vec3 g111 = vec3(gx1.w, gy1.w, gz1.w);\n  vec4 n0 = tisqrt(vec4(dot(g000,g000), dot(g010,g010), dot(g100,g100), dot(g110,g110)));\n  g000 *= n0.x; g010 *= n0.y; g100 *= n0.z; g110 *= n0.w;\n  vec4 n1 = tisqrt(vec4(dot(g001,g001), dot(g011,g011), dot(g101,g101), dot(g111,g111)));\n  g001 *= n1.x; g011 *= n1.y; g101 *= n1.z; g111 *= n1.x;\n  float n000 = dot(g000, Pf0); float n100 = dot(g100, vec3(Pf1.x, Pf0.yz));\n  float n010 = dot(g010, vec3(Pf0.x, Pf1.y, Pf0.z)); float n110 = dot(g110, vec3(Pf1.xy, Pf0.z));\n  float n001 = dot(g001, vec3(Pf0.xy, Pf1.z)); float n101 = dot(g101, vec3(Pf1.x, Pf0.y, Pf1.z));\n  float n011 = dot(g011, vec3(Pf0.x, Pf1.yz)); float n111 = dot(g111, Pf1);\n  vec3 fx = fade3(Pf0);\n  vec4 nz = mix(vec4(n000,n100,n010,n110), vec4(n001,n101,n011,n111), fx.z);\n  vec2 ny = mix(nz.xy, nz.zw, fx.y);\n  return 2.2*mix(ny.x, ny.y, fx.x);\n}\nuniform vec2 u_dirv; // (cos, sin) of the direction quarter-turn\nvoid main(){\n  vec2 st = gl_FragCoord.xy/u_res - 0.5;\n  st.x *= u_res.x/u_res.y;\n  st = mat2(u_dirv.x, u_dirv.y, -u_dirv.y, u_dirv.x)*st;\n  float time = u_t*0.85;\n  vec2 uv = st*(1.0/(2.0*ZOOM)) + 0.5;\n  // gl_FragCoord runs bottom-up, so the source's y flip is already this way up\n  float noiseX = cnoise(vec3(uv*u_nscale + vec2(0.0, 74.8572), time*0.3));\n  float noiseY = cnoise(vec3(uv*u_nscale + vec2(203.91282, 10.0), time*0.3));\n  uv += vec2(noiseX*2.0, noiseY)*u_warp;\n  float noiseA = cnoise(vec3(uv*18.0 + vec2(344.91282, 0.0), time*0.3))\n               + cnoise(vec3(uv*39.6 + vec2(723.937, 0.0), time*0.4))*0.5;\n  uv += noiseA*0.02;\n  uv.y -= 0.09;\n  float xf = (sin(time) + 1.0)*0.5;\n  vec2 texUv = uv*GRAIN_SCALE;\n  float d0 = mix(texture2D(u_noise, texUv).r - 0.5, texture2D(u_noise, vec2(texUv.x, 1.0-texUv.y)).g - 0.5, xf)*GRAIN_STRENGTH;\n  texUv += vec2(63.861, 368.937);\n  float d1 = mix(texture2D(u_noise, texUv).r - 0.5, texture2D(u_noise, vec2(texUv.x, 1.0-texUv.y)).g - 0.5, xf)*GRAIN_STRENGTH;\n  texUv += vec2(453.163, 1649.808);\n  float d3 = mix(texture2D(u_noise, texUv).r - 0.5, texture2D(u_noise, vec2(texUv.x, 1.0-texUv.y)).g - 0.5, xf)*GRAIN_STRENGTH;\n  uv += d0;\n  vec2 stF = uv*u_nscale;\n  vec2 q = vec2(fbm4(stF*0.5 + u_wind*time));\n  vec2 r = vec2(fbm4(stF + q + vec2(0.3, 9.2) + 0.15*time), fbm4(stF + q + vec2(8.3, 0.8) + 0.126*time));\n  float fv = fbm4(stF + r - q);\n  float full = (fv + 0.6*fv*fv + 0.7*fv + 0.5)*0.5;\n  full = pow(full, 0.55)*FBM_STRENGTH;\n  float blurR = BLUR_RADIUS*1.5;\n  vec2 uvA = uv + vec2((full-0.5)*1.2) + vec2(0.0, 0.025) + d0;\n  float snA = noise2(uvA*2.0 + vec2(0.0, time*0.5))*3.0;\n  float lA = pow(smoothstep(snA - 1.2*blurR, snA + 1.2*blurR, (uvA.y - 0.5)*5.0 + 0.5), 0.8);\n  vec2 uvB = uv + vec2((full-0.5)*0.85) + vec2(0.0, 0.025) + d1;\n  float snB = noise2(uvB*4.0 + vec2(293.0, time))*2.8;\n  float lB = pow(smoothstep(snB - 0.9*blurR, snB + 0.9*blurR, (uvB.y - 0.6)*5.0 + 0.5), 0.9);\n  vec2 uvC = uv + vec2((full-0.5)*1.1) + d3;\n  float snC = noise2(uvC*6.0 + vec2(153.0, time*1.2))*2.6;\n  float lC = smoothstep(snC - 0.7*blurR, snC + 0.7*blurR, (uvC.y - 0.9)*6.0 + 0.5);\n  // 天气产品式垂直大气渐变：地平线(h=0)=high 最亮，天顶(h=1)=low 最深。\n  float h = clamp(gl_FragCoord.y / u_res.y, 0.0, 1.0);\n  vec3 grad = mix(u_high, u_mid, smoothstep(0.0, 0.45, h));\n  grad = mix(grad, u_main, smoothstep(0.40, 0.75, h));\n  grad = mix(grad, u_low, smoothstep(0.70, 1.0, h));\n  // 基础设计法：域扭曲 fbm 三层云密度(lA/lB/lC)叠在天空上；原 Blue sky 云受光近白。\n  // 云受光用 u_cloud（昼近白/夜暗navy），背光轻压向天顶色，随时间漂移。\n  vec3 col = mix(grad, mix(grad, u_low, 0.4), (1.0 - lA) * 0.35);\n  col = mix(col, mix(grad, u_cloud, 0.8), lA * lB * 0.8);\n  col = mix(col, u_cloud, lA * lC * 0.3);\n  gl_FragColor = vec4(col, 1.0);\n}"

const DEFAULT_SPEED = 22
const START_T = 60.3531021440194
const SCALE = 45
const DISTORTION = 50
const SWIRL = 40
const DIRECTION = 0
const GRAIN = 7
const MAX_WIDTH = 2560
const NOISE_SEED = 0x5bf0b3

type Resources = {
  program: WebGLProgram
  vertex: WebGLShader
  fragment: WebGLShader
  buffer: WebGLBuffer
  texture: WebGLTexture
  resolution: WebGLUniformLocation | null
  time: WebGLUniformLocation | null
}

/** mulberry32：固定种子的伪随机。纹理与颗粒用它，视觉基线才可逐帧复现。 */
function seededRandom(seed: number): () => number {
  let state = seed >>> 0
  return () => {
    state = (state + 0x6d2b79f5) >>> 0
    let t = state
    t = Math.imul(t ^ (t >>> 15), t | 1)
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61)
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296
  }
}

function compile(gl: WebGLRenderingContext, kind: number, source: string): WebGLShader {
  const shader = gl.createShader(kind)
  if (!shader) throw new Error('Could not create the Sky shader.')
  gl.shaderSource(shader, source)
  gl.compileShader(shader)
  if (!gl.getShaderParameter(shader, gl.COMPILE_STATUS)) {
    const message = gl.getShaderInfoLog(shader) || 'Sky shader failed to compile.'
    gl.deleteShader(shader)
    throw new Error(message)
  }
  return shader
}

export function SkyGradient({
  className,
  palette,
  speed = DEFAULT_SPEED,
}: {
  className?: string
  palette: SkyPalette
  speed?: number
}) {
  const canvasRef = useRef<HTMLCanvasElement>(null)
  const grainRef = useRef<HTMLDivElement>(null)
  const controlsRef = useRef({ speed })
  const syncRef = useRef<(() => void) | null>(null)
  const [failed, setFailed] = useState(false)
  controlsRef.current = { speed }

  // useLayoutEffect：色板变更（深浅切换）需要在 View Transitions 拍新快照前
  // 同步重绘新时段，否则圆揭开时画布还是旧色。布局副作用在 flushSync 提交内
  // 同步跑，赶得上快照；首屏也因此无闪烁。
  useLayoutEffect(() => {
    const canvas = canvasRef.current
    if (!canvas) return
    // preserveDrawingBuffer=true 是硬需求：View Transitions 抓快照走合成器，
    // 默认 false 时 present 后缓冲被清，快照里画布整块发黑（深浅切换 sky「消失」的根因）。
    const gl = (canvas.getContext('webgl', { alpha: false, antialias: false, preserveDrawingBuffer: true }) ||
      canvas.getContext('experimental-webgl', { alpha: false, antialias: false, preserveDrawingBuffer: true })) as WebGLRenderingContext | null
    if (!gl) {
      setFailed(true)
      return
    }

    let resources: Resources | null = null
    let frame = 0
    let last = 0
    let t = START_T
    let visible = true
    let lost = false
    let disposed = false
    const motion = window.matchMedia('(prefers-reduced-motion: reduce)')

    const destroy = () => {
      if (!resources || lost) {
        resources = null
        return
      }
      gl.deleteTexture(resources.texture)
      gl.deleteBuffer(resources.buffer)
      gl.deleteProgram(resources.program)
      gl.deleteShader(resources.vertex)
      gl.deleteShader(resources.fragment)
      resources = null
    }

    const setup = () => {
      const vertex = compile(gl, gl.VERTEX_SHADER, VERTEX_SHADER)
      let fragment: WebGLShader | null = null
      let program: WebGLProgram | null = null
      let buffer: WebGLBuffer | null = null
      let texture: WebGLTexture | null = null
      try {
        fragment = compile(gl, gl.FRAGMENT_SHADER, FRAGMENT_SHADER)
        program = gl.createProgram()
        if (!program) throw new Error('Could not create the Sky program.')
        gl.attachShader(program, vertex)
        gl.attachShader(program, fragment)
        gl.bindAttribLocation(program, 0, 'p')
        gl.linkProgram(program)
        if (!gl.getProgramParameter(program, gl.LINK_STATUS)) {
          throw new Error(gl.getProgramInfoLog(program) || 'Sky program failed to link.')
        }
        gl.useProgram(program)
        buffer = gl.createBuffer()
        if (!buffer) throw new Error('Could not create the Sky geometry.')
        gl.bindBuffer(gl.ARRAY_BUFFER, buffer)
        gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 3, -1, -1, 3]), gl.STATIC_DRAW)
        gl.enableVertexAttribArray(0)
        gl.vertexAttribPointer(0, 2, gl.FLOAT, false, 0, 0)

        texture = gl.createTexture()
        if (!texture) throw new Error('Could not create the Sky noise texture.')
        gl.activeTexture(gl.TEXTURE0)
        gl.bindTexture(gl.TEXTURE_2D, texture)
        const noise = new Uint8Array(256 * 256 * 4)
        const next = seededRandom(NOISE_SEED)
        for (let i = 0; i < 256 * 256; i++) {
          const value = (next() * 256) | 0
          noise[i * 4] = value
          noise[i * 4 + 1] = value
          noise[i * 4 + 2] = value
          noise[i * 4 + 3] = 255
        }
        gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, 256, 256, 0, gl.RGBA, gl.UNSIGNED_BYTE, noise)
        gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.REPEAT)
        gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.REPEAT)
        gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR)
        gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR)
        gl.uniform1i(gl.getUniformLocation(program, 'u_noise'), 0)
        gl.uniform3fv(gl.getUniformLocation(program, 'u_main'), palette.tones.main)
        gl.uniform3fv(gl.getUniformLocation(program, 'u_low'), palette.tones.low)
        gl.uniform3fv(gl.getUniformLocation(program, 'u_mid'), palette.tones.mid)
        gl.uniform3fv(gl.getUniformLocation(program, 'u_high'), palette.tones.high)
        gl.uniform3fv(gl.getUniformLocation(program, 'u_cloud'), palette.tones.cloud)
        gl.uniform1f(gl.getUniformLocation(program, 'u_nscale'), 0.35 + (SCALE / 100) * 1.15)
        gl.uniform1f(gl.getUniformLocation(program, 'u_warp'), (DISTORTION / 100) * 0.47)
        gl.uniform1f(gl.getUniformLocation(program, 'u_wind'), (SWIRL / 100) * 0.36)
        const angle = ((DIRECTION % 4) * Math.PI) / 2
        gl.uniform2f(gl.getUniformLocation(program, 'u_dirv'), Math.cos(angle), Math.sin(angle))
        resources = {
          program,
          vertex,
          fragment,
          buffer,
          texture,
          resolution: gl.getUniformLocation(program, 'u_res'),
          time: gl.getUniformLocation(program, 'u_t'),
        }
      } catch (error) {
        if (texture) gl.deleteTexture(texture)
        if (buffer) gl.deleteBuffer(buffer)
        if (program) gl.deleteProgram(program)
        if (fragment) gl.deleteShader(fragment)
        gl.deleteShader(vertex)
        throw error
      }
    }

    const paint = () => {
      if (!resources || lost || disposed || canvas.width < 1 || canvas.height < 1) return
      gl.useProgram(resources.program)
      gl.viewport(0, 0, canvas.width, canvas.height)
      gl.uniform2f(resources.resolution, canvas.width, canvas.height)
      gl.uniform1f(resources.time, t)
      gl.drawArrays(gl.TRIANGLES, 0, 3)
    }

    const active = () =>
      !disposed && !lost && !!resources && visible && !document.hidden && !motion.matches &&
      controlsRef.current.speed > 0
    const tick = (now: number) => {
      frame = 0
      if (!active()) {
        last = 0
        return
      }
      if (last) {
        t += Math.min(0.05, (now - last) / 1000) * (Math.min(100, Math.max(0, controlsRef.current.speed)) / 100) * 1.2
      }
      last = now
      paint()
      frame = requestAnimationFrame(tick)
    }
    const sync = () => {
      if (!active()) {
        if (frame) cancelAnimationFrame(frame)
        frame = 0
        last = 0
        paint()
      } else if (!frame) {
        last = 0
        paint()
        frame = requestAnimationFrame(tick)
      }
    }
    syncRef.current = sync

    const resize = () => {
      const rect = canvas.getBoundingClientRect()
      const dpr = Math.min(3, window.devicePixelRatio || 1)
      const scale = Math.min(1, MAX_WIDTH / Math.max(1, rect.width * dpr))
      const width = Math.max(1, Math.round(rect.width * dpr * scale))
      const height = Math.max(1, Math.round(rect.height * dpr * scale))
      if (canvas.width !== width) canvas.width = width
      if (canvas.height !== height) canvas.height = height
      paint()
    }
    const onLost = (event: Event) => {
      event.preventDefault()
      lost = true
      resources = null
      if (frame) cancelAnimationFrame(frame)
      frame = 0
      last = 0
      setFailed(true)
    }
    const onRestored = () => {
      lost = false
      try {
        setup()
        setFailed(false)
        resize()
        sync()
      } catch (error) {
        console.warn('Sky WebGL restore failed:', error)
        setFailed(true)
      }
    }
    canvas.addEventListener('webglcontextlost', onLost)
    canvas.addEventListener('webglcontextrestored', onRestored)
    const observer = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(resize)
    observer?.observe(canvas)
    window.addEventListener('resize', resize)
    const intersection =
      typeof IntersectionObserver === 'undefined' ? null : new IntersectionObserver(([entry]) => {
        visible = entry.isIntersecting
        sync()
      })
    intersection?.observe(canvas)
    document.addEventListener('visibilitychange', sync)
    motion.addEventListener('change', sync)

    if (GRAIN > 0 && grainRef.current) {
      const tile = document.createElement('canvas')
      tile.width = tile.height = 256
      const context = tile.getContext('2d')
      if (context) {
        const image = context.createImageData(256, 256)
        const next = seededRandom(NOISE_SEED ^ 0x9e3779b9)
        for (let i = 0; i < image.data.length; i += 4) {
          const value = Math.round((next() + next()) * 127.5)
          image.data[i] = value
          image.data[i + 1] = value
          image.data[i + 2] = value
          image.data[i + 3] = 255
        }
        context.putImageData(image, 0, 0)
        grainRef.current.style.backgroundImage = 'url(' + tile.toDataURL() + ')'
      }
    }

    try {
      setup()
      setFailed(false)
      resize()
      sync()
    } catch (error) {
      console.warn('Sky WebGL unavailable:', error)
      setFailed(true)
    }

    return () => {
      disposed = true
      syncRef.current = null
      if (frame) cancelAnimationFrame(frame)
      observer?.disconnect()
      intersection?.disconnect()
      window.removeEventListener('resize', resize)
      document.removeEventListener('visibilitychange', sync)
      motion.removeEventListener('change', sync)
      canvas.removeEventListener('webglcontextlost', onLost)
      canvas.removeEventListener('webglcontextrestored', onRestored)
      destroy()
    }
    // palette 引用是 SKY_PALETTES 里的模块级常量：时段切换=新引用=重建资源，其余不重跑。
  }, [palette])

  useEffect(() => {
    syncRef.current?.()
  }, [speed])

  return (
    <div className={className} aria-hidden="true">
      <canvas
        ref={canvasRef}
        style={{
          position: 'absolute',
          inset: 0,
          display: 'block',
          width: '100%',
          height: '100%',
          visibility: failed ? 'hidden' : 'visible',
        }}
      />
      {GRAIN > 0 && (
        <div
          ref={grainRef}
          style={{
            position: 'absolute',
            inset: 0,
            pointerEvents: 'none',
            backgroundSize: '256px 256px',
            imageRendering: 'pixelated',
            mixBlendMode: 'overlay',
            opacity: GRAIN / 200,
          }}
        />
      )}
    </div>
  )
}
