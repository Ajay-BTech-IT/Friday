/* A realistic 3D head for the face page (three.js).
   - Head: the "Face Cap" scan with the 52 standard facial shape keys (ARKit names), so every expression is a mix of them.
   - Skin: the scan's texture tinted to a human skin tone + procedural pores / grain / blotches / sub-surface warmth.
   - Hair and eyebrows: strands grown in many thin shells over the scalp (strand lighting with a Kajiya-Kay highlight).
   The page's expression numbers (chosen by the assistant) and the lip-sync values are mapped onto the shape keys here;
   nothing in this file decides WHAT is expressed. */
import * as THREE from "three";
import { GLTFLoader } from "three/addons/loaders/GLTFLoader.js";
import { KTX2Loader } from "three/addons/loaders/KTX2Loader.js";
import { MeshoptDecoder } from "three/addons/libs/meshopt_decoder.module.js";
import { RoomEnvironment } from "three/addons/environments/RoomEnvironment.js";

const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
const GLSL_NOISE = /* glsl */ `
float h11(float n){ return fract(sin(n * 127.1) * 43758.5453); }
float h13(vec3 p){ p = fract(p * 0.3183099 + .1); p *= 17.0; return fract(p.x * p.y * p.z * (p.x + p.y + p.z)); }
vec2 h22(vec2 p){ p = vec2(dot(p, vec2(127.1, 311.7)), dot(p, vec2(269.5, 183.3))); return fract(sin(p) * 43758.5453); }
float vnoise(vec3 p){ vec3 i = floor(p), f = fract(p); f = f * f * (3.0 - 2.0 * f);
  return mix(mix(mix(h13(i), h13(i + vec3(1,0,0)), f.x), mix(h13(i + vec3(0,1,0)), h13(i + vec3(1,1,0)), f.x), f.y),
             mix(mix(h13(i + vec3(0,0,1)), h13(i + vec3(1,0,1)), f.x), mix(h13(i + vec3(0,1,1)), h13(i + vec3(1,1,1)), f.x), f.y), f.z); }
float fbm(vec3 p){ float a = .5, s = 0.; for (int i = 0; i < 4; i++){ s += a * vnoise(p); p *= 2.03; a *= .5; } return s; }
`;

const GLSL_HAIRLINE = /* glsl */ `
// where the hair starts: high on the forehead, down to above the ears at the temples, low at the nape (q: the head's 0..1 box,
// z runs downward in this mesh, so height = 1 - z). Returns 0..1 coverage and the azimuth around the head.
float hairCover(vec3 q, out float phi){
  float hgt = 1. - q.z;
  vec2 c = vec2(q.x - .5, q.y - .36);
  phi = atan(c.x, c.y);                                              // 0 = front
  float a = abs(phi);
  float thr = .80 - .28 * smoothstep(.55, 1.35, a) - .18 * smoothstep(1.4, 2.6, a);
  thr += (vnoise(vec3(phi * 7., hgt * 9., 1.)) - .5) * .045;
  return smoothstep(thr - .015, thr + .03, hgt);
}`;

/* ---------- skin: tinted scan + pores, grain, blotches, warm sub-surface look ---------- */
function makeSkin(oldMat, bbox, skin) {
  const m = new THREE.MeshPhysicalMaterial({
    map: oldMat.map, color: new THREE.Color(skin), roughness: .58, metalness: 0,
    sheen: .7, sheenRoughness: .55, sheenColor: new THREE.Color(0xff9a80),
    clearcoat: .08, clearcoatRoughness: .45, envMapIntensity: .55,
  });
  m.userData.uniforms = {
    uMin: { value: bbox.min.clone() }, uSize: { value: bbox.getSize(new THREE.Vector3()) },
    uFlush: { value: 0.5 }, uLip: { value: new THREE.Color(0xb3524f) }, uBrowCol: { value: new THREE.Color(0x2b1b12) }, uDebug: { value: 0 },
  };
  m.onBeforeCompile = (sh) => {
    Object.assign(sh.uniforms, m.userData.uniforms);
    sh.vertexShader = sh.vertexShader
      .replace("#include <common>", "#include <common>\nvarying vec3 vBind;")
      .replace("#include <begin_vertex>", "#include <begin_vertex>\nvBind = position;");
    sh.fragmentShader = sh.fragmentShader
      .replace("#include <common>", `#include <common>
varying vec3 vBind; uniform vec3 uMin, uSize, uLip, uBrowCol; uniform float uFlush, uDebug;
${GLSL_NOISE}
${GLSL_HAIRLINE}
float skinH(vec3 q){ return vnoise(q * 46.0) * .55 + vnoise(q * 95.0) * .3 + (1.0 - smoothstep(.0, .2, abs(vnoise(q * 160.0) - .5))) * .15; }
`)
      .replace("#include <map_fragment>", `#include <map_fragment>
{
  vec3 q = (vBind - uMin) / uSize;                    // 0..1 across the head in its bind pose
  float blot = fbm(q * 7.0 + 3.0), fine = vnoise(q * 34.0) * .6 + vnoise(q * 70.0) * .4;
  vec3 warm = vec3(1.04, .94, .90), ruddy = vec3(1.08, .86, .82);
  diffuseColor.rgb *= mix(vec3(1.0), mix(warm, ruddy, smoothstep(.35, .75, blot)), .55);
  diffuseColor.rgb *= .965 + .07 * fine;
  // lips: a warmer, redder band around the mouth (position of the mouth in the bind pose)
  vec2 lm = (vec2(q.x, 1. - q.z) - vec2(.5, .214)) / vec2(.15, .034);
  float lip = 1.0 - smoothstep(.62, 1.0, length(lm));
  diffuseColor.rgb = mix(diffuseColor.rgb, uLip * (.9 + .2 * fine), lip * .78);
  // brows: the strand layer is drawn on top, this only darkens the skin under it
  { float ph; float cov = hairCover(q, ph); diffuseColor.rgb = mix(diffuseColor.rgb, vec3(.045, .028, .02), cov * .92); }   // the scalp under the hair is dark
  if (uDebug > .5) {                                   // calibration: a stripe every .05 of height, a red one every .1; blue stripes for x
    float yb = fract(q.z * 20.), xb = fract(q.x * 20.);
    diffuseColor.rgb = vec3(.75);
    if (yb < .12) diffuseColor.rgb = (fract(q.z * 10.) < .06 || fract(q.z * 10.) > .94) ? vec3(1., 0., 0.) : vec3(.1, .1, .1);
    if (xb < .06 && yb > .12) diffuseColor.rgb = (abs(q.x - .5) < .03) ? vec3(0., .8, 0.) : vec3(0., .2, 1.);
  }
}`)
      .replace("#include <normal_fragment_maps>", `#include <normal_fragment_maps>
{
  vec3 q = (vBind - uMin) / uSize;
  float hgt = skinH(q);
  vec3 dpdx = dFdx(vViewPosition), dpdy = dFdy(vViewPosition);
  float dhx = dFdx(hgt), dhy = dFdy(hgt);
  vec3 r1 = cross(dpdy, normal), r2 = cross(normal, dpdx);
  float det = dot(dpdx, r1);
  vec3 grad = sign(det) * (dhx * r1 + dhy * r2);
  normal = normalize(abs(det) * normal - .0022 * grad);
}`);
  };
  return m;
}

/* ---------- hair / eyebrows: strand shells ---------- */
const SHELL_VERT = /* glsl */ `
#include <common>
#include <morphtarget_pars_vertex>
uniform float uShell, uLenTop, uLenSide, uLenBrow, uMode, uUnit;
uniform vec3 uMin, uSize;
varying vec3 vN, vNormalW, vWorld;
varying float vH;
void main(){
  #include <morphinstance_vertex>
  vec3 objectNormal = vec3(normal);
  #include <morphnormal_vertex>
  vec3 transformed = vec3(position);
  #include <morphtarget_vertex>
  vec3 q = (position - uMin) / uSize; vN = q;
  float hgt = 1. - q.z;                                   // 0 chin .. 1 top of the head
  float top = smoothstep(.80, .97, hgt);
  float len = uMode < .5 ? mix(uLenSide, uLenTop, top) : uLenBrow;
  vec3 flow = normalize(vec3(0., -.15, 1.));              // strands fall down (+z) and a little backwards
  vec3 dir = normalize(mix(normalize(objectNormal), flow, uMode < .5 ? (1. - top) * .45 : .0));
  transformed += dir * uShell * len * uUnit;
  transformed.z += uShell * uShell * len * uUnit * (uMode < .5 ? .12 : .0);
  vH = uShell;
  vNormalW = normalize(mat3(modelMatrix) * objectNormal);
  vec4 wp = modelMatrix * vec4(transformed, 1.); vWorld = wp.xyz;
  gl_Position = projectionMatrix * viewMatrix * wp;
}`;
const SHELL_FRAG = /* glsl */ `
precision highp float;
uniform vec3 uMin, uSize, uRoot, uTip, uKey, uFill, uRim, uKeyCol, uFillCol, uRimCol;
uniform float uShell, uMode, uDensity, uDebug;
varying vec3 vN, vNormalW, vWorld; varying float vH;
${GLSL_NOISE}
${GLSL_HAIRLINE}
void main(){
  vec3 q = vN;
  float alpha, rnd, lenFactor = 1.0;
  if (uMode < .5) {
    // ---- scalp: strands in (azimuth, height) around the crown; the hairline depends on the direction around the head
    float hgt = 1. - q.z;
    float phi; float cover = hairCover(q, phi);
    float rad = length(vec2(q.x - .5, q.y - .36));
    float thr = 0.;
    if (uDebug > 1.5) { gl_FragColor = vec4(hgt, cover, thr, 1.); return; }
    if (cover < .02) discard;
    vec2 uv = vec2(phi * uDensity * (.45 + .55 * smoothstep(.0, .35, rad * 2.2)), hgt * uDensity * .62);
    vec2 cell = floor(uv), f = fract(uv) - .5;
    vec2 jit = h22(cell) - .5; f -= jit * .55;
    rnd = h22(cell + 7.3).x;
    float length_ = (.55 + .45 * rnd) * cover;
    float th = (.46 - vH * .34);
    alpha = step(vH, length_) * (1. - smoothstep(th * .6, th, length(f)));
    lenFactor = rnd;
  } else {
    // ---- eyebrows: two arched bands in the bind pose
    float hgt = 1. - q.z;
    vec2 p = vec2(abs(q.x - .5), hgt);
    float t = clamp((p.x - .075) / .205, 0., 1.);
    float arch = .575 + .035 * sin(3.14159 * pow(t, .8)) - .014 * t;       // low at the inner end, peak about two thirds out, then falling
    float thick = .0085 + .0105 * (1. - t) * (1. - t * .4);
    float band = 1. - smoothstep(.0, thick, abs(p.y - arch));
    float span = smoothstep(.07, .095, p.x) * (1. - smoothstep(.255, .29, p.x));
    float inFront = step(.55, q.y);
    vec2 uv = vec2(p.x * 700., hgt * 700.);
    vec2 cell = floor(uv), f = fract(uv) - .5; f -= (h22(cell) - .5) * .5;
    rnd = h22(cell + 3.1).x;
    float dens = band * span * inFront;
    alpha = step(vH, (.45 + .55 * rnd) * clamp(dens * 1.5, 0., 1.)) * (1. - smoothstep(.22, .42 - vH * .15, length(f)));
  }
  if (alpha < .5) discard;
  if (uDebug > .5) { gl_FragColor = vec4(1., 0., 0., 1.); return; }

  // ---- strand colour: dark root, lighter tip, per-strand variation
  vec3 col = mix(uRoot, uTip, smoothstep(.0, 1., vH)) * (.8 + .4 * rnd);
  vec3 N = normalize(vNormalW), V = normalize(cameraPosition - vWorld);
  vec3 T = normalize(cross(cross(N, vec3(0., -1., 0.)), N) + 1e-4);   // strand direction (down the head)
  float ao = mix(.35, 1., vH);                                        // roots are shadowed by the strands above
  float dK = max(dot(N, uKey), 0.) * .7 + .22, dF = max(dot(N, uFill), 0.) * .3, dR = pow(1. - max(dot(N, V), 0.), 2.) * .5;
  vec3 diffuse = col * (uKeyCol * dK + uFillCol * dF + uRimCol * dR * .5) * ao;
  // Kajiya-Kay highlight: a band across the strands that moves with the light
  vec3 H = normalize(uKey + V); float tH = dot(T, H); float sp = pow(sqrt(max(0., 1. - tH * tH)), 70.) * (.25 + .6 * rnd);
  vec3 spec = uKeyCol * sp * .22 + uRimCol * pow(sqrt(max(0., 1. - dot(T, normalize(uRim + V)) * dot(T, normalize(uRim + V)))), 50.) * .12;
  gl_FragColor = vec4(diffuse + spec, 1.);
  #include <colorspace_fragment>
}`;

function makeShells(head, bbox, mode, count, opts) {
  const group = new THREE.Group(), mats = [];
  const size = bbox.getSize(new THREE.Vector3());
  for (let i = 1; i <= count; i++) {
    const m = new THREE.ShaderMaterial({
      vertexShader: SHELL_VERT, fragmentShader: SHELL_FRAG, alphaToCoverage: false, side: THREE.FrontSide,
      uniforms: {
        uShell: { value: i / count }, uMode: { value: mode }, uMin: { value: bbox.min.clone() }, uSize: { value: size.clone() },
        uLenTop: { value: opts.lenTop }, uLenSide: { value: opts.lenSide }, uLenBrow: { value: opts.lenBrow ?? .02 }, uUnit: { value: size.x },
        uDensity: { value: opts.density }, uRoot: { value: new THREE.Color(opts.root) }, uTip: { value: new THREE.Color(opts.tip) },
        uKey: { value: new THREE.Vector3(-.5, .7, .6).normalize() }, uFill: { value: new THREE.Vector3(.7, .1, .6).normalize() }, uRim: { value: new THREE.Vector3(0, .5, -1).normalize() },
        uKeyCol: { value: new THREE.Color(1, .93, .84).multiplyScalar(1.5) }, uFillCol: { value: new THREE.Color(.55, .65, .9) }, uRimCol: { value: new THREE.Color(.3, .8, 1) },
        uDebug: { value: 0 },
      },
    });
    const mesh = new THREE.Mesh(head.geometry, m);
    mesh.morphTargetInfluences = head.morphTargetInfluences;           // the strands deform with the face
    mesh.morphTargetDictionary = head.morphTargetDictionary;
    mesh.frustumCulled = false; mesh.renderOrder = 2;
    group.add(mesh); mats.push(m);
  }
  head.parent.add(group);
  group.position.copy(head.position); group.quaternion.copy(head.quaternion); group.scale.copy(head.scale);
  return { group, mats };
}

/* ---------- the avatar ---------- */
export async function createAvatar(canvas, opts = {}) {
  const renderer = new THREE.WebGLRenderer({ canvas, antialias: true, alpha: true, powerPreference: "high-performance" });
  renderer.setClearColor(0x000000, 0); renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
  renderer.toneMapping = THREE.ACESFilmicToneMapping; renderer.toneMappingExposure = 1.15;
  const scene = new THREE.Scene();
  const pm = new THREE.PMREMGenerator(renderer);
  scene.environment = pm.fromScene(new RoomEnvironment(), .04).texture; scene.environmentIntensity = .55;

  const ktx2 = new KTX2Loader().setTranscoderPath(new URL("./vendor/addons/libs/basis/", import.meta.url).href).detectSupport(renderer);
  const gltf = await new GLTFLoader().setKTX2Loader(ktx2).setMeshoptDecoder(MeshoptDecoder)
    .loadAsync(new URL("./vendor/models/facecap.glb", import.meta.url).href);
  const root = new THREE.Group(); root.add(gltf.scene); scene.add(root);
  gltf.scene.updateMatrixWorld(true);

  const head = gltf.scene.getObjectByName("mesh_2"), teeth = gltf.scene.getObjectByName("mesh_3");
  const eyeMeshes = [gltf.scene.getObjectByName("mesh_0"), gltf.scene.getObjectByName("mesh_1")];
  const eyeNodes = [gltf.scene.getObjectByName("eyeLeft"), gltf.scene.getObjectByName("eyeRight")];
  // box of the resting shape only: three.js would also stretch it over every shape key's extreme position
  const bbox = new THREE.Box3().setFromBufferAttribute(head.geometry.attributes.position);

  head.material = makeSkin(head.material, bbox, opts.skin || "#d8a88c");
  const eyeCol = { value: new THREE.Color(opts.eye || "#6a4a2a") };
  for (const e of eyeMeshes) {
    const em = new THREE.MeshPhysicalMaterial({ map: e.material.map, roughness: .12, clearcoat: 1, clearcoatRoughness: .05, envMapIntensity: 1.6 });
    em.onBeforeCompile = (sh) => {
      sh.uniforms.uEyeCol = eyeCol;
      sh.fragmentShader = sh.fragmentShader.replace("#include <common>", "#include <common>\nuniform vec3 uEyeCol;")
        .replace("#include <map_fragment>", `#include <map_fragment>
{ float l = dot(diffuseColor.rgb, vec3(.333)); float iris = smoothstep(.78, .42, l) * smoothstep(.04, .16, l);
  diffuseColor.rgb = mix(diffuseColor.rgb, uEyeCol * (.55 + 1.1 * l), iris * .9); }`);
    };
    e.material = em;
  }
  teeth.material = new THREE.MeshStandardMaterial({ map: teeth.material.map, roughness: .35, color: 0xf1e6d8 });

  const hair = makeShells(head, bbox, 0, 34, { lenTop: .2, lenSide: .075, density: 210, root: opts.hairRoot || "#120a07", tip: opts.hairTip || "#3a2417" });
  const brows = makeShells(head, bbox, 1, 7, { lenTop: 0, lenSide: 0, lenBrow: .02, density: 0, root: "#1c120c", tip: "#33200f" });

  // lights: warm key, cool fill, a rim in the colour the assistant's face glow has
  const key = new THREE.DirectionalLight(0xffe7d2, 2.1); key.position.set(-2.2, 3, 4); scene.add(key);
  const fill = new THREE.DirectionalLight(0xbfd6ff, .55); fill.position.set(3, 0, 3); scene.add(fill);
  const rim = new THREE.DirectionalLight(0x66ccff, 1.6); rim.position.set(0, 1.5, -3); scene.add(rim);
  const rimColor = new THREE.Color();

  // camera frames the head
  const wb = new THREE.Box3().setFromObject(head, true), c = wb.getCenter(new THREE.Vector3()), s = wb.getSize(new THREE.Vector3());
  const cam = new THREE.PerspectiveCamera(24, 1, .1, 100);
  const dist = (Math.max(s.y, s.z) * 1.32) / (2 * Math.tan(THREE.MathUtils.degToRad(12)));
  cam.position.set(c.x, c.y + s.y * .02, c.z + dist + s.z * .5); cam.lookAt(c.x, c.y + s.y * .02, c.z);
  const pivot = new THREE.Vector3(c.x, c.y - s.y * .25, c.z);          // the head turns around the neck, not its middle
  root.position.sub(pivot); const rig = new THREE.Group(); rig.position.copy(pivot); scene.remove(root); rig.add(root); scene.add(rig);

  function resize() {
    const w = canvas.clientWidth || canvas.parentElement.clientWidth, h = canvas.clientHeight || canvas.parentElement.clientHeight;
    renderer.setSize(w, h, false); cam.aspect = w / h; cam.updateProjectionMatrix();
  }
  resize(); window.addEventListener("resize", resize); if (window.ResizeObserver) new ResizeObserver(resize).observe(canvas);

  const dict = head.morphTargetDictionary, infl = head.morphTargetInfluences;
  const set = (name, v) => { const i = dict[name]; if (i !== undefined) infl[i] = clamp(v, 0, 1); };
  const lerpTo = (name, v, k) => { const i = dict[name]; if (i !== undefined) infl[i] += (clamp(v, 0, 1) - infl[i]) * k; };

  /* p: the face numbers (springs already applied), ls: lip-sync values from the voice, blink 0..1 */
  function update(p, ls, blink, now) {
    const up = Math.max(0, p.brow_raise), down = Math.max(0, -p.brow_raise), tilt = p.brow_tilt, asym = p.brow_asym;
    set("browInnerUp", up * .9 + Math.max(0, -tilt) * .95);
    set("browOuterUp_L", up * .85 - asym * .6);          // asym > 0: the right brow is the higher one, the left drops
    set("browOuterUp_R", up * .85 + asym * .6);
    set("browDown_L", down + Math.max(0, tilt) * .9 + Math.max(0, -asym) * .45);
    set("browDown_R", down + Math.max(0, tilt) * .9 + Math.max(0, asym) * .45);

    const close = clamp(Math.max(blink, (.82 - p.eye_open) * 2.4, p.lid_droop * .62), 0, 1), wide = clamp((p.eye_open - .82) * 4, 0, 1);
    set("eyeBlink_L", close); set("eyeBlink_R", close);
    set("eyeWide_L", wide); set("eyeWide_R", wide);
    set("eyeSquint_L", p.eye_smile * .8); set("eyeSquint_R", p.eye_smile * .8);
    set("cheekSquint_L", p.eye_smile * .9 + Math.max(0, p.mouth_curve) * .25); set("cheekSquint_R", p.eye_smile * .9 + Math.max(0, p.mouth_curve) * .25);

    const sm = Math.max(0, p.mouth_curve), fr = Math.max(0, -p.mouth_curve), sk = p.mouth_skew;
    set("mouthSmile_L", sm * .9 + Math.max(0, -sk) * .5); set("mouthSmile_R", sm * .9 + Math.max(0, sk) * .5);
    set("mouthDimple_L", sm * .5); set("mouthDimple_R", sm * .5);
    set("mouthFrown_L", fr * .9 + Math.max(0, sk) * .2); set("mouthFrown_R", fr * .9 + Math.max(0, -sk) * .2);
    set("noseSneer_L", fr * .25 + down * .15); set("noseSneer_R", fr * .25 + down * .15);
    const stretch = clamp((p.mouth_width - .5) * 1.4 + ls.wide * .55, 0, 1);
    set("mouthStretch_L", stretch * .7); set("mouthStretch_R", stretch * .7);

    const open = clamp(Math.max(p.mouth_open, ls.open), 0, 1), round = clamp(p.mouth_round + ls.round * .95, 0, 1);
    set("jawOpen", open * .62); set("mouthLowerDown_L", open * .5 + ls.teeth * .25); set("mouthLowerDown_R", open * .5 + ls.teeth * .25);
    set("mouthUpperUp_L", open * .25 + ls.teeth * .45); set("mouthUpperUp_R", open * .25 + ls.teeth * .45);
    set("mouthFunnel", round * .85 * (1 - open * .3)); set("mouthPucker", round * .6 + clamp(.5 - p.mouth_width, 0, .5) * .5);
    const press = p.lip_press * (1 - open);
    set("mouthPress_L", press * .9); set("mouthPress_R", press * .9); set("mouthRollUpper", press * .3); set("mouthRollLower", press * .3);
    set("mouthClose", clamp((ls.loud < .02 && p.mouth_open < .03 ? .0 : 0), 0, 1));
    set("jawLeft", Math.max(0, -sk) * .15); set("jawRight", Math.max(0, sk) * .15);
    set("tongueOut", 0);

    // eyes look where the face numbers say (the nodes carry the eyeballs; the lids follow with the shape keys)
    const gx = p.gaze_x, gy = p.gaze_y;
    for (const n of eyeNodes) { n.rotation.y = gx * .38; n.rotation.x = gy * .32; }
    set("eyeLookUp_L", -gy); set("eyeLookUp_R", -gy); set("eyeLookDown_L", gy); set("eyeLookDown_R", gy);

    // head: roll, nod, turn (+ breathing)
    const breathe = Math.sin(now / 1900) * .004;
    rig.rotation.set(-p.head_nod * .12 + breathe, p.head_turn * .42, -p.head_tilt * Math.PI / 180);

    // rim light + shell lighting follow the glow colour of the face
    rimColor.setHSL(p.hue / 360, .9, .6);
    rim.color.copy(rimColor); rim.intensity = .5 + 2.0 * p.glow;
    for (const m of [...hair.mats, ...brows.mats]) m.uniforms.uRimCol.value.copy(rimColor).multiplyScalar(.25 + .5 * p.glow);
    renderer.render(scene, cam);
  }

  /* where the facial landmarks are in the head's own 0..1 box (found from the geometry, used to place hair and brows) */
  function landmarks() {
    const pos = head.geometry.attributes.position, n = pos.count, mn = bbox.min, sz = bbox.getSize(new THREE.Vector3());
    const q = (i) => [(pos.getX(i) - mn.x) / sz.x, (pos.getY(i) - mn.y) / sz.y, (pos.getZ(i) - mn.z) / sz.z];
    let nose = 0, chin = 0, top = 0;
    for (let i = 0; i < n; i++) { if (pos.getZ(i) > pos.getZ(nose)) nose = i; if (pos.getY(i) < pos.getY(chin) && pos.getZ(i) > mn.z + sz.z * .55) chin = i; if (pos.getY(i) > pos.getY(top)) top = i; }
    const moved = (name) => {                       // centre of the vertices that the shape key moves the most
      const a = head.geometry.morphAttributes.position[dict[name]], d = [];
      for (let i = 0; i < n; i++) d.push([Math.hypot(a.getX(i), a.getY(i), a.getZ(i)), i]);
      d.sort((x, y) => y[0] - x[0]); const top = d.slice(0, 60), c = [0, 0, 0];
      for (const [, i] of top) { const v = q(i); c[0] += v[0] / top.length; c[1] += v[1] / top.length; c[2] += v[2] / top.length; }
      return c.map((v) => +v.toFixed(3));
    };
    const eyePos = eyeNodes.map((e) => { const w = e.getWorldPosition(new THREE.Vector3()); const l = head.worldToLocal(w); return [(l.x - mn.x) / sz.x, (l.y - mn.y) / sz.y, (l.z - mn.z) / sz.z].map((v) => +v.toFixed(3)); });
    const f = (v) => v.map((x) => +x.toFixed(3));
    return { nose: f(q(nose)), chin: f(q(chin)), top: f(q(top)), eyes: eyePos, mouthJaw: moved("jawOpen"), browInner: moved("browInnerUp"), browOuterL: moved("browOuterUp_L"), mouthSmile: moved("mouthSmile_L"), eyeBlink: moved("eyeBlink_L"),
             box: bbox.getSize(new THREE.Vector3()).toArray(), preciseWorld: new THREE.Box3().setFromObject(head, true).getSize(new THREE.Vector3()).toArray().map((v) => +v.toFixed(3)) };
  }

  return {
    update, resize, landmarks, THREE, head, hair, brows, rig, cam, renderer, scene,
    setSkin(hex) { head.material.color.set(hex); },
    setEye(hex) { eyeCol.value.set(hex); },
    setHair(root, tip) { for (const m of hair.mats) { m.uniforms.uRoot.value.set(root); m.uniforms.uTip.value.set(tip); } },
    debug(mode) { hair.group.visible = brows.group.visible = mode !== 1; head.material.userData.uniforms.uDebug.value = mode; for (const m of [...hair.mats, ...brows.mats]) m.uniforms.uDebug.value = mode > 1 ? mode - 1 : 0; },
  };
}
