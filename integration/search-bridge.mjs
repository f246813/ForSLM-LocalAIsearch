// Thin bridge: all search, reranking and page extraction come from agent-search.
import { Server } from '../upstream/agent-search/node_modules/@modelcontextprotocol/server/dist/index.mjs';
import { ResearchServer } from '../upstream/agent-search/dist/research.js';
import { fetchAndConvertToMarkdown, isUrlReadFailure } from '../upstream/agent-search/dist/url-reader.js';

const server = new Server({ name: 'forslm-local-search', version: '1.0.0' }, { capabilities: { tools: {} } });
const query = process.argv[2];
const emit = data => process.stdout.write(JSON.stringify(data) + '\n');
try {
  const research = new ResearchServer();
  research.setServer(server);
  const response = await research.processSearch({ searchedKeywords: [query], lang: 'all' });
  const result = JSON.parse(response.content[0].text).searchResults?.[0];
  if (!result || result.error || !result.results.length) throw new Error(result?.error || '搜索引擎未返回可用结果');
  emit({ type: 'ranked', candidateCount: result.candidateCount, selectedCount: result.results.length });
  const sources = await Promise.all(result.results.map(async (item, index) => {
    let content = '';
    try {
      content = await fetchAndConvertToMarkdown(server, item.link, 12000, { maxLength: 2600 });
      if (isUrlReadFailure(content)) content = '';
    } catch (error) { console.error('Page read failed:', item.link, String(error)); }
    return { ...item, id: index + 1, content: content || item.snippet, excerptOnly: !content };
  }));
  emit({ type: 'result', query, candidateCount: result.candidateCount, sources });
  process.exit(0); // Upstream search uses timeout promises that otherwise keep the CLI alive.
} catch (error) {
  console.error(String(error));
  emit({ type: 'error', message: String(error) });
  process.exit(1);
}
