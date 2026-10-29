# vendor — 화면이 쓰는 외부 JavaScript (오프라인용 사본)

3D 보기와 학습 그래프가 인터넷(CDN) 없이도 동작하도록 받은 파일을 그대로 넣어 두었습니다. 고치지 않았습니다.
서버가 `/vendor/…` 로 내줍니다 (`main.py` `serve_vendor`).

| 경로 | 원본 | 버전 | 라이선스 |
|---|---|---|---|
| `three/build/three.module.js`, `three/examples/jsm/controls/OrbitControls.js`, `three/examples/jsm/loaders/{STLLoader,ColladaLoader,TGALoader}.js` | [three.js](https://github.com/mrdoob/three.js) (npm `three`) | 0.160.0 | MIT — [three/LICENSE](three/LICENSE) |
| `urdf-loader/{URDFLoader,URDFClasses}.js` | [urdf-loaders](https://github.com/gkjohnson/urdf-loaders) (npm `urdf-loader`) | 0.12.6 | Apache-2.0 (npm 패키지 `license` 항목, 패키지에 라이선스 파일이 없어 원본 저장소 참조) |
| `chart/chart.umd.js` | [Chart.js](https://github.com/chartjs/Chart.js) (npm `chart.js`) | 4.4.1 | MIT — [chart/LICENSE.md](chart/LICENSE.md) |
