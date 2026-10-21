import React from 'react';
import {AbsoluteFill, Audio, Composition, Sequence, interpolate, registerRoot, spring, staticFile, useCurrentFrame, useVideoConfig} from 'remotion';

type Scene = {title: string; narration: string; labels: string[]; visual: string; frames: number};
type Props = {title: string; scenes: Scene[]; duration: number};
const colors = ['#ed6147', '#e9a23b', '#d5ba4d', '#60a889', '#428dde', '#746bc1'];
const clamp = {extrapolateLeft: 'clamp', extrapolateRight: 'clamp'} as const;

function Drawing({scene}: {scene: Scene}) {
  const f = useCurrentFrame();
  const {fps} = useVideoConfig();
  const p = spring({frame: f, fps, config: {damping: 23}});
  const points = Array.from({length: 18}, (_, i) => ({x: 345 + (i % 6) * 95, y: 110 + Math.floor(i / 6) * 130}));
  return <svg viewBox="0 0 1100 550" style={{width: '100%', height: '100%', overflow: 'visible'}}>
    {scene.visual === 'spectrum' ? <>
      <path d="M420 130 L600 440 L240 440 Z" fill="#d5e7ee" stroke="#427c9b" strokeWidth="3"/>
      <path d={`M70 280 L${70 + 305 * p} 280`} stroke="#e9b954" strokeWidth="20"/>
      {colors.map((c, i) => <line key={c} x1="480" y1="280" x2={480 + 490 * p} y2={180 + i * 40} stroke={c} strokeWidth="13" opacity={p}/>)}
      <text x="75" y="355" fontSize="30" fill="#283f58">白色阳光</text>
    </> : scene.visual === 'scatter' ? <>
      {points.map((pt, i) => <circle key={i} cx={pt.x} cy={pt.y + Math.sin(f / fps + i) * 8} r="12" fill="#8ea7bc" opacity=".55"/>)}
      <path d="M35 270 L1050 270" stroke="#e98264" strokeWidth="5" strokeDasharray="16 12"/>
      {Array.from({length: 12}, (_, i) => {
        const a = i * Math.PI / 6;
        const r = ((f * 5 + i * 29) % 330);
        return <g key={i}><line x1="550" y1="270" x2={550 + Math.cos(a) * 310 * p} y2={270 + Math.sin(a) * 230 * p} stroke="#81b8ef" strokeWidth="3" opacity=".55"/><circle cx={550 + Math.cos(a) * r} cy={270 + Math.sin(a) * r * .72} r="7" fill="#2675ce"/></g>;
      })}
      <circle cx="550" cy="270" r="25" fill="#24445d"/>
    </> : scene.visual === 'path' ? <>
      <path d="M70 465 Q550 220 1030 465" stroke="#86ae9c" strokeWidth="40" fill="none"/>
      <path d="M70 440 Q550 195 1030 440" stroke="#c9dfe9" strokeWidth="55" opacity=".65" fill="none"/>
      <circle cx="95" cy="165" r="58" fill="#efa352"/>
      <path d="M150 180 L960 345" stroke="#df7856" strokeWidth="8" strokeDasharray={`${850 * p} 900`}/>
      {Array.from({length: 8}, (_, i) => <circle key={i} cx={170 + i * 95} cy={190 + i * 19 - ((f * 2 + i * 11) % 100)} r="6" fill="#5799dd" opacity=".8"/>)}
    </> : <>
      {(scene.labels.length ? scene.labels : [scene.title]).slice(0, 3).map((label, i, all) => {
        const x = all.length === 1 ? 550 : 210 + i * 340;
        const reveal = spring({frame: f - i * 8, fps, config: {damping: 22}});
        return <g key={i} opacity={reveal} transform={`translate(0,${(1 - reveal) * 35})`}>
          {i > 0 && <path d={`M${x - 220} 255 L${x - 125} 255`} stroke="#94a5ad" strokeWidth="4"/>}
          <circle cx={x} cy="255" r="108" fill={['#d9e8f3', '#f6dfba', '#d9e8df'][i]} />
          <circle cx={x} cy="255" r={88 + Math.sin(f / fps + i) * 4} fill="none" stroke={['#4382b3', '#c68a38', '#5b927a'][i]} strokeWidth="2"/>
          <text x={x} y="265" textAnchor="middle" fontSize="27" fontWeight="600" fill="#263e54">{label.slice(0, 10)}</text>
        </g>;
      })}
    </>}
    {['spectrum', 'scatter', 'path'].includes(scene.visual) && scene.labels.slice(0, 3).map((s, i) => <text key={i} x={100 + i * 370} y="525" fontSize="26" fill="#40617c">{s}</text>)}
  </svg>;
}

function Shot({scene, index, total}: {scene: Scene; index: number; total: number}) {
  const f = useCurrentFrame();
  const {fps} = useVideoConfig();
  const alpha = Math.min(interpolate(f, [0, 10], [0, 1], clamp), interpolate(f, [scene.frames - 8, scene.frames], [1, 0], clamp));
  const chunks = scene.narration.match(/[^，。！？；、]{1,19}[，。！？；、]?/g) || [scene.narration];
  const subtitle = chunks[Math.min(chunks.length - 1, Math.floor(f / scene.frames * chunks.length))];
  return <AbsoluteFill style={{background: '#f5f2e9', color: '#233f59', padding: '60px 90px', fontFamily: 'PingFang SC, Microsoft YaHei, sans-serif'}}>
    <div style={{display: 'flex', justifyContent: 'space-between', fontSize: 21, letterSpacing: 3, color: '#788b98'}}><span>看见原理 / SCIENCE EXPLAINED</span><span>{String(index + 1).padStart(2, '0')} / {String(total).padStart(2, '0')}</span></div>
    <div style={{opacity: alpha, transform: `translateY(${(1 - spring({frame: f, fps, config: {damping: 25}})) * 18}px)`}}>
      <h1 style={{fontSize: 54, margin: '37px 0 12px', lineHeight: 1.3, fontWeight: 650}}>{scene.title}</h1>
      <div style={{width: 65, height: 5, background: '#d29b49', marginBottom: 10}}/>
      <div style={{height: 470, maxWidth: 1080, margin: '0 auto'}}><Drawing scene={scene}/></div>
    </div>
    <div style={{position: 'absolute', bottom: 70, left: 100, right: 100, fontSize: 30, textAlign: 'center', color: '#304e66'}}>{subtitle}</div>
    <div style={{position: 'absolute', left: 0, right: 0, bottom: 0, height: 6, background: '#dfe5e4'}}><div style={{height: '100%', background: '#4789b6', width: `${100 * f / scene.frames}%`}}/></div>
  </AbsoluteFill>;
}

const Video: React.FC<Props> = ({scenes}) => {
  let offset = 0;
  return <AbsoluteFill><Audio src={staticFile('narration.wav')}/>{scenes.map((scene, i) => {
    const start = offset; offset += scene.frames;
    return <Sequence key={i} from={start} durationInFrames={scene.frames}><Shot scene={scene} index={i} total={scenes.length}/></Sequence>;
  })}</AbsoluteFill>;
};

registerRoot(() => <Composition id="LocalExplainer" component={Video} width={1280} height={720} fps={30} durationInFrames={1350} defaultProps={{title: '', scenes: [], duration: 45}} calculateMetadata={({props}) => ({durationInFrames: Math.round(props.duration * 30)})}/>);
