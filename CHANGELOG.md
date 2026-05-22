# 🪵 Changelog

## [0.7.0](https://github.com/EOSC-Data-Commons/data-commons-search/tree/v0.7.0) - 2026-05-22

### ⚙️ Continuous Integration

- Publish docker image to ghcr.io container registry - ([86613fa](https://github.com/EOSC-Data-Commons/data-commons-search/commit/86613faaf552077239e262057269838df2d7bf79))
- Fix docker push - ([28af4fc](https://github.com/EOSC-Data-Commons/data-commons-search/commit/28af4fc99ac05d3c8931432d7c991841cd8564ea))
- Fix repo by hardcoding repo name since github actions ridiculously do not have a toLowercase function but requires image name to be lowercase - ([1c9dd31](https://github.com/EOSC-Data-Commons/data-commons-search/commit/1c9dd31c1d8f39c941a7f4c74e0de79f9fe12e42))
- Build docker for arm64 - ([482c936](https://github.com/EOSC-Data-Commons/data-commons-search/commit/482c93604fdff89d9180b9d415fae9ca2acb73aa))
- Improve the gh action workflow to handle when a new tag is published using `cargo release patch` - ([154903b](https://github.com/EOSC-Data-Commons/data-commons-search/commit/154903b823bf702e8f7cd23d1b68d65bae38241f))
- Fix aarch env - ([76f0682](https://github.com/EOSC-Data-Commons/data-commons-search/commit/76f068299f20043992b7a5985e3d04b378c221ea))
- Fix arm build - ([9ec60ed](https://github.com/EOSC-Data-Commons/data-commons-search/commit/9ec60ed3f946765f184d08c4d672caca1e0f8a4a))
- Merge docker manifest for amd and arm builds - ([2935540](https://github.com/EOSC-Data-Commons/data-commons-search/commit/29355405b29f4b8e38e5bf67f66449d2d50f1711))
- Build docker for arm64 - ([b6b690f](https://github.com/EOSC-Data-Commons/data-commons-search/commit/b6b690f2c7744d85ebf09688f7862b65e6a0e8e1))
- Fix build binary for arm64 - ([f1cea41](https://github.com/EOSC-Data-Commons/data-commons-search/commit/f1cea41147b38d6f477aa59e5e1b5faa8a6765c2))
- Fix build binary for arm64 - ([09830bb](https://github.com/EOSC-Data-Commons/data-commons-search/commit/09830bbbfcf3e87fb56c0b07236c00fad7fcda5d))
- Put back building arm on ubuntu-latest - ([6397c89](https://github.com/EOSC-Data-Commons/data-commons-search/commit/6397c8907df8a030ae851656facbce7c40314c05))
- Try fix aarch linux - ([ed16dd4](https://github.com/EOSC-Data-Commons/data-commons-search/commit/ed16dd436c0460494ce5667dfa11eec4dc910cce))
- Try fix aarch linux - ([1567919](https://github.com/EOSC-Data-Commons/data-commons-search/commit/156791940fc13fac780bce540c571c15c8588f8d))
- Try fix aarch linux using native arm runner - ([be8f68b](https://github.com/EOSC-Data-Commons/data-commons-search/commit/be8f68b10be381919c48c3ceba4b9f76f3ae9c56))
- Fix tests and improve build workflow - ([1cfb0da](https://github.com/EOSC-Data-Commons/data-commons-search/commit/1cfb0daa65e3f83fa90ed6cae8d6c84934b4bbca))
- Fix build workflow - ([670a8d2](https://github.com/EOSC-Data-Commons/data-commons-search/commit/670a8d24bb729f8f1137e6b864fd8f61c56aa450))
- Ci - ([ba4c7a6](https://github.com/EOSC-Data-Commons/data-commons-search/commit/ba4c7a6d427b801693b78585188795d8a7b975ca))
- Ci: comment `test_get_relevant_tools`, waiting for the filemetrix API to be more stable
feat: put back capability to filter on creators - ([fa35306](https://github.com/EOSC-Data-Commons/data-commons-search/commit/fa35306fe9b9bbf29aa674d3422d99f4a1b20b72))
- Checkout 0.4.2 of eodcpoc repo - ([5aa61ea](https://github.com/EOSC-Data-Commons/data-commons-search/commit/5aa61ea67ac6924e13643c72509fd547ff493880))
- Update workflow to only push docker images for new git tags - ([8ce5b99](https://github.com/EOSC-Data-Commons/data-commons-search/commit/8ce5b9902f4ab080c9697dd2b7fdd0963a64b6a3))

### ⛰️ Features

- Add logging of full conversation and response to a file for the search API - ([f78707f](https://github.com/EOSC-Data-Commons/data-commons-search/commit/f78707f9e3d2f6e0d35d7e0a6e81f2a110837341))
- Replace the search query to Zenodo by a query to a local OpenSearch service - ([9d75058](https://github.com/EOSC-Data-Commons/data-commons-search/commit/9d7505886ee6f05932ee5eb7cb6be6f9e012b390))
- Add config through CLI - ([c1fd052](https://github.com/EOSC-Data-Commons/data-commons-search/commit/c1fd052680ff056faa96009df1cae3ddd98e0e20))
- Support OpenSearch embedding search, using fastembed-rs to compute embeddings - ([31391f9](https://github.com/EOSC-Data-Commons/data-commons-search/commit/31391f9dd7fe6dc3482d8068bb9e349a852be744))
- Capable of handling start/end dates - ([eb91ff1](https://github.com/EOSC-Data-Commons/data-commons-search/commit/eb91ff12e538bff2d5c8de302212ba529994d596))
- Enable using the e-infra.cz LLM provider - ([de58c85](https://github.com/EOSC-Data-Commons/data-commons-search/commit/de58c8521f34ba5ad88b3ad678dc76983aa49c28))
- Add CORS accept layer and serve `src/webapp` static files, add mermaid sequence diagram for wp4 - ([45d3119](https://github.com/EOSC-Data-Commons/data-commons-search/commit/45d31199af59b88a032a45241c38b1529356ca7b))
- Serve frontend webapp on / and /search - ([afe13d9](https://github.com/EOSC-Data-Commons/data-commons-search/commit/afe13d90c5646dd621156d9a95e38861e0473711))
- Migrate output stream format to use the AG-UI protocol - ([b896529](https://github.com/EOSC-Data-Commons/data-commons-search/commit/b896529e337f88e2e06958ab68c3c8f4f63ae96d))
- Migrate from rust to python - ([56ac30e](https://github.com/EOSC-Data-Commons/data-commons-search/commit/56ac30e1810c8d64ef1c5af0751c9a2e4aeac42b))
- Add date in system prompt, log token usage, improve logging - ([3c85495](https://github.com/EOSC-Data-Commons/data-commons-search/commit/3c854957f4c2938729347f7b9ded843cf5390d07))
- Add `get_dataset_files` MCP tool, improve gh action workflow to build the frontend from source - ([52bf12f](https://github.com/EOSC-Data-Commons/data-commons-search/commit/52bf12f5d3cbf958edd1f37b0dbe02bb44bde9fe))
- Add calls to get relevant tools - ([68adeac](https://github.com/EOSC-Data-Commons/data-commons-search/commit/68adeac1ef8140be2619c5a0e05331fcf31b0888))
- Those are rookie numbers, you've gotta pump those numbers up. Increase the limit for opensearch to 100, and rerank 20 hit. Now only retrieve file extensions and relevant tools for reranked hits - ([f2efe7e](https://github.com/EOSC-Data-Commons/data-commons-search/commit/f2efe7ea39b9d544ede1b23605339f6d63f9d722))
- Add timestamp to AG-UI streamed events, and add `asyncio.sleep(0)` to make sure events are properly flushed - ([c4b85a4](https://github.com/EOSC-Data-Commons/data-commons-search/commit/c4b85a47e687b0c86fe78947f2eb85f5ff0769ab))
- Add harvest url and repo to opensearch _source - ([1928b01](https://github.com/EOSC-Data-Commons/data-commons-search/commit/1928b01b406d3f3cc042084688bccfbf748ee2ae))
- Replace Starlette with FastAPI for defining the API - ([f032b62](https://github.com/EOSC-Data-Commons/data-commons-search/commit/f032b620e705596813a1dd9f604732d222994518))
- Use LiteLLM API instead of OpenWebUI API for e-infra CZ provider - ([749625a](https://github.com/EOSC-Data-Commons/data-commons-search/commit/749625ae487eafe74752f1755365360a107d4236))
- Add authentication through EGI OpenID Connect - ([4e876da](https://github.com/EOSC-Data-Commons/data-commons-search/commit/4e876da6f4ca8f1de38d63359fed8e4d5185133f))
- Add tracing with langfuse - ([54117d5](https://github.com/EOSC-Data-Commons/data-commons-search/commit/54117d58954ea9bc41c7466d659b57a4555bd6ae))
- Add support for refresh token to refresh the access token - ([a4d107a](https://github.com/EOSC-Data-Commons/data-commons-search/commit/a4d107a6ff3d25295d1f83da769e72856e46dc33))
- Add rate limites on chat endpoint - ([dcf761f](https://github.com/EOSC-Data-Commons/data-commons-search/commit/dcf761f142ab223d560c102a95adec0554431481))
- Store users, conversations and messages in postgres db - ([88eca47](https://github.com/EOSC-Data-Commons/data-commons-search/commit/88eca475218ee23eb09ee331d0fb7b503a356945))

### 🎨 Styling

- Format - ([ce109ed](https://github.com/EOSC-Data-Commons/data-commons-search/commit/ce109edf29c435f8866d07b53c27a124e9b5fb69))
- Refactor - ([3f29ad8](https://github.com/EOSC-Data-Commons/data-commons-search/commit/3f29ad8eacfaaa5dfdaf2aab5e41ab6cda90179f))

### 🐛 Bug Fixes

- Make sure URL is always filled when only doi provided - ([c5605b9](https://github.com/EOSC-Data-Commons/data-commons-search/commit/c5605b968b59e59ae1254d9601f8d672d79c3260))
- Dockerfile to work with onnx runtime - ([eaeed01](https://github.com/EOSC-Data-Commons/data-commons-search/commit/eaeed011d2be60439a903ffae01511855955a703))
- Stream error as message, add support for openrouter provider - ([2c45705](https://github.com/EOSC-Data-Commons/data-commons-search/commit/2c457057d24772c29c3c2a73a1f41b25e0c4746a))
- Fix port to 8000 in docker image - ([a2c83d5](https://github.com/EOSC-Data-Commons/data-commons-search/commit/a2c83d51b1824c61c22ad95d31e5d81804c6ea5c))
- Fix envs in dockerfile entrypoint - ([a5687a2](https://github.com/EOSC-Data-Commons/data-commons-search/commit/a5687a285a3e218e85a51bf0b13e746e614d40f0))
- Server host in docker - ([ff319a5](https://github.com/EOSC-Data-Commons/data-commons-search/commit/ff319a5ae1e80722ad0e261ff48b973b6499dcf1))
- Fix race condition when using multiple workers - ([6b4b57a](https://github.com/EOSC-Data-Commons/data-commons-search/commit/6b4b57a2f84c07a079a73ecb0156ff3af4af9e1b))
- Remove `asyncio.sleep(0)` and update the frontend webapp build - ([1bd4497](https://github.com/EOSC-Data-Commons/data-commons-search/commit/1bd449785a0668b8430bb3e70a459f3d6b6a067d))
- Add header for nginx buffering - ([b85106e](https://github.com/EOSC-Data-Commons/data-commons-search/commit/b85106e0492e491d1e2da9e57371c174a6f02b8c))
- Pydantic private fields - ([d9673ca](https://github.com/EOSC-Data-Commons/data-commons-search/commit/d9673ca55f5ca3b94b94a46fdb5009ac9f5332e9))
- Made harvest_url and repo optional in OpenSearch hits source - ([0ba7d51](https://github.com/EOSC-Data-Commons/data-commons-search/commit/0ba7d51ace24e25b48f88ae410cad6e400177a01))
- Fix imports for new module name - ([3660d66](https://github.com/EOSC-Data-Commons/data-commons-search/commit/3660d662aa208842cac59c92e44e4a0a722ac94b))
- Disable getting each reranked dataset file extensions and potential relevant tools as this is done directly from the frontend when a user show interest for a dataset (e.g. clicks on it) - ([75e0471](https://github.com/EOSC-Data-Commons/data-commons-search/commit/75e0471d518ff0c88e8ec47c29e43ec6de54f4ee))
- Fix logs - ([d311210](https://github.com/EOSC-Data-Commons/data-commons-search/commit/d311210cc2d012fca415b8e6d94f83e6829ac726))
- Return empty results when OpenSearch throw an error - ([a65d7bb](https://github.com/EOSC-Data-Commons/data-commons-search/commit/a65d7bb2ce1427e3932502a8f52dbef1fbfeb3ab))
- Improve OpenSearch author filtering - ([1d3cee2](https://github.com/EOSC-Data-Commons/data-commons-search/commit/1d3cee25b9e8de68aa0ec1db6de6edde645cacb2))
- Update cesnet LLM URL to use the litellm endpoint - ([4b9334d](https://github.com/EOSC-Data-Commons/data-commons-search/commit/4b9334d0efc79ec7a2374d1152e8e12e21ee94b1))
- Fix: fix langfuse callback handler
refactor: move langfuse init inside context - ([02f5e90](https://github.com/EOSC-Data-Commons/data-commons-search/commit/02f5e90fedaa859f65ba9c6cc6cd716fe8ad3bd4))
- Langfuse from v3 to v4 - ([21a9d25](https://github.com/EOSC-Data-Commons/data-commons-search/commit/21a9d25363e7489a96aa78a8cf7cd4b2dc4cbf49))
- Fix opensearch `_harvest_url` field parsing - ([800be23](https://github.com/EOSC-Data-Commons/data-commons-search/commit/800be236468e96317e7acd06d487ecea48b4cbb7))

### 📚 Documentation

- Delete webapp as we dont serve it anymore from this API, now using SSR, redirect root path to `/docs` swagger UI, improve instruction to send request with authenticated user access token - ([8596313](https://github.com/EOSC-Data-Commons/data-commons-search/commit/8596313fa79a8601c3944412e8dd7273dcc20179))

### 🚜 Refactor

- Introduce SearchWorkflow struct with functions for each step of the workflow, this enables to easily use the same workflow in the streaming and non-streaming implementations. Improve release process - ([5a6954c](https://github.com/EOSC-Data-Commons/data-commons-search/commit/5a6954c18a5d129221c8f7c1f433d1841729ee29))
- Rename `api.rs` to `search.rs` - ([f1e2e52](https://github.com/EOSC-Data-Commons/data-commons-search/commit/f1e2e52d3548ea609991aa7a24d71ea8bb1b91b0))
- Rename client to mcp_client - ([3f49028](https://github.com/EOSC-Data-Commons/data-commons-search/commit/3f490284dc5e1e990d760fde5369fd9243bd3ac1))
- Use official `opensearch` crate instead of reqwest to interact with the OpenSearch service - ([b73bb7b](https://github.com/EOSC-Data-Commons/data-commons-search/commit/b73bb7bcce742bb47896aa41f5b583fed9092d1f))
- Fmt - ([bc18fc1](https://github.com/EOSC-Data-Commons/data-commons-search/commit/bc18fc180d65bb67b65402f3be3a71cd70ed9133))
- Add utils.rs file - ([97f92ba](https://github.com/EOSC-Data-Commons/data-commons-search/commit/97f92baf737f66a1f40aee6df79f818f97dd7db9))
- Fmt - ([2780fca](https://github.com/EOSC-Data-Commons/data-commons-search/commit/2780fca6ae32511c12d8940dc23352d17a0eeac1))
- Use structured content when returning MCP tools results, improve opensearch query and change mistral provider to mistralai - ([03cf66e](https://github.com/EOSC-Data-Commons/data-commons-search/commit/03cf66e7473179825cbc7d67091c2e5176e5dad8))
- Comments and upgrade rmcp to 0.6 - ([e14f0ad](https://github.com/EOSC-Data-Commons/data-commons-search/commit/e14f0ad456720b26392c2e09907424beb85fccfe))
- Rename `src/search.rs` to `arc/chat.rs` - ([31ceb87](https://github.com/EOSC-Data-Commons/data-commons-search/commit/31ceb8783216cc4b1113522f4bf76c5116f021d9))
- Fmgt - ([14e0885](https://github.com/EOSC-Data-Commons/data-commons-search/commit/14e088528dc228671febaafa2edf8437c0321747))
- Add usage debug log - ([87eaf22](https://github.com/EOSC-Data-Commons/data-commons-search/commit/87eaf2279411b07c680d7ea3cee90fb9da154e73))
- Import shuffle in tests - ([8d9dd57](https://github.com/EOSC-Data-Commons/data-commons-search/commit/8d9dd57f987586ea25579daced53c2dc18ba3b4d))
- Rename search_data mcp tool to search_datasets - ([47d1ba9](https://github.com/EOSC-Data-Commons/data-commons-search/commit/47d1ba9d1758fde2d38eef6e76554d57eb2f7445))
- Use a class for `UserInfo` - ([82a07ba](https://github.com/EOSC-Data-Commons/data-commons-search/commit/82a07bac846d8ad385115b15c4eb1645a98161ca))
- Use `UserInfo` class for /auth/user endpoint - ([3ce8725](https://github.com/EOSC-Data-Commons/data-commons-search/commit/3ce87252e32860eb9a5343b49d193014b1a9a3bb))

### 🛠️ Miscellaneous Tasks

- Cargo update to fix advisory - ([4355b64](https://github.com/EOSC-Data-Commons/data-commons-search/commit/4355b646346903e66d802182b88cd97047dd56b1))
- Upgrade rmcp version from 0.3 to 0.5 - ([a3a3d7f](https://github.com/EOSC-Data-Commons/data-commons-search/commit/a3a3d7faa0b6669348f8f78ae00d2f4d072e54f3))
- Improve OpenAPI schema definition - ([0d4ecbb](https://github.com/EOSC-Data-Commons/data-commons-search/commit/0d4ecbbdfa63cff7c748dcb5842e3e28f39e830a))
- Upgrade rmcp to 0.6 - ([a033ece](https://github.com/EOSC-Data-Commons/data-commons-search/commit/a033ece887f7d0da6986b547032fe68632dd4adb))
- Cargo update - ([d8b087a](https://github.com/EOSC-Data-Commons/data-commons-search/commit/d8b087a5bb8b6d2c01b5655ba6f5085d5949cf89))
- Update lock - ([64cf164](https://github.com/EOSC-Data-Commons/data-commons-search/commit/64cf164ec6f8d74cfa9bb49f20e7a18183cb3c3d))
- Upgrade rmcp dependency - ([d6213e2](https://github.com/EOSC-Data-Commons/data-commons-search/commit/d6213e223bb1503b28ff47cde2cabb87d5ed16b4))
- Readme - ([494c9f9](https://github.com/EOSC-Data-Commons/data-commons-search/commit/494c9f93979ffccf8a2ba94ebb0a9fb32745d49c))
- Readme - ([7c1be86](https://github.com/EOSC-Data-Commons/data-commons-search/commit/7c1be86d5196727d46395bbbb1d55bbd40a1d9ad))
- Add repo and harvest_url to search hit source model - ([473a5ef](https://github.com/EOSC-Data-Commons/data-commons-search/commit/473a5ef059841dcb42b8920a76434d615696e74b))
- Add debugging prints - ([a57c499](https://github.com/EOSC-Data-Commons/data-commons-search/commit/a57c49920fdd08f76a64002c91d78040f7e0cf2c))
- Rename _harvest_url field to _harvestUrl - ([d719471](https://github.com/EOSC-Data-Commons/data-commons-search/commit/d7194713935cc7bfc0169500a30983fa794f813f))
- Rename repo and package from `data-commons-mcp` to `data-commons-search` - ([de74dfb](https://github.com/EOSC-Data-Commons/data-commons-search/commit/de74dfbbf25655568029c784322887373621d330))
- Rename `src/data_commons_mcp` to `src/data_commons_search` - ([cc0ac59](https://github.com/EOSC-Data-Commons/data-commons-search/commit/cc0ac592875c8150baf644fa5909603a016643b4))
- Remove dirty prints - ([2f03976](https://github.com/EOSC-Data-Commons/data-commons-search/commit/2f03976e17c56c4c6d701a27f178dafbfe6e2082))
- Add debug logs - ([3e749c2](https://github.com/EOSC-Data-Commons/data-commons-search/commit/3e749c27e32dafebc0928ecfd9df434ad9b09bca))
- Update website - ([8008668](https://github.com/EOSC-Data-Commons/data-commons-search/commit/80086688afe63d197fa84ec8ce6133dc417857e0))
- Upgrade dependencies - ([acd2e66](https://github.com/EOSC-Data-Commons/data-commons-search/commit/acd2e66abcaa9a46ab4a18680d24f54b1fb255bb))
- Improve reranking prompt to enable follow up questions - ([59cd807](https://github.com/EOSC-Data-Commons/data-commons-search/commit/59cd807c866be2ec8a38f1b2ed5e53c2e9bdb385))
- Update opensearch index used, pass all creators field instead of just `creatorName` to the client - ([e5b26f6](https://github.com/EOSC-Data-Commons/data-commons-search/commit/e5b26f6621a80df94940d0256301f3594da3b6da))
- Add script to generate SQL schema from sqlalchemy models - ([0f2e6e7](https://github.com/EOSC-Data-Commons/data-commons-search/commit/0f2e6e7b3d37070c90aa1a02db8c53e4bd7ad19c))
- Comments - ([939e499](https://github.com/EOSC-Data-Commons/data-commons-search/commit/939e49997cb964ef841fa88a3281d235ac168681))

### 🧪 Testing

- Add basic tests for search and MCP endpoints, providers are now included in model, pass bind-address from CLI args to enable config of where it can be deployed - ([b9b0e8a](https://github.com/EOSC-Data-Commons/data-commons-search/commit/b9b0e8a9c2896eb7ef9381af12dfb6bda96bb79f))
- Add benchmark.py script to test different conditions (e.g. diff models) - ([5f5e47d](https://github.com/EOSC-Data-Commons/data-commons-search/commit/5f5e47d785466a5ce46211c4506ed35678edbddd))

<!-- generated by git-cliff -->
