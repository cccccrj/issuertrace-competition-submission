/** I-06：对外交付的平台不显示内部的模型、提示词、内部编号与原始报错。
 *
 *  读源码而不是渲染：要钉住的是「模板里不再写这些」这条性质。注释（HTML 注释与
 *  JS 注释）不算——生产构建会去掉它们，而它们正是写给维护者的地方。
 */
import { readdirSync, readFileSync, statSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { describe, expect, it } from 'vitest'

const SRC = join(dirname(fileURLToPath(import.meta.url)), '..', '..')

function files(dir) {
  return readdirSync(dir).flatMap((name) => {
    const path = join(dir, name)
    if (name === '__tests__') return []
    if (statSync(path).isDirectory()) return files(path)
    return /\.(vue|js)$/.test(name) ? [path] : []
  })
}

/** 去掉注释后剩下的、会出现在页面或构建产物里的文本。 */
function visible(text) {
  return text
    .replace(/<!--[\s\S]*?-->/g, '')
    .replace(/\/\*[\s\S]*?\*\//g, '')
    .replace(/(^|[^:'"`])\/\/.*$/gm, '$1')
}

const SOURCES = files(SRC).map((path) => ({ path: path.slice(SRC.length + 1), text: visible(readFileSync(path, 'utf8')) }))

describe('页面上不出现内部实现', () => {
  it('不出现模型厂商与型号', () => {
    for (const { path, text } of SOURCES) {
      expect(text, path).not.toMatch(/qwen|deepseek|zhipu|openai|claude|gpt-|zhizengzeng/i)
    }
  })

  it('不称呼「模型」「大模型」「AI」「LLM」——统一说「系统研判」', () => {
    for (const { path, text } of SOURCES) {
      // AI_GENERATED 是生产方式的码，说的是文章本身，不是本平台。
      expect(text.replace(/AI_GENERATED/g, ''), path).not.toMatch(/模型|LLM|\bAI\b/)
    }
  })

  it('不渲染模型名、提示词版本、处理器版本与原始报错字段', () => {
    for (const { path, text } of SOURCES) {
      expect(text, path).not.toMatch(/\.(model_name|prompt_version|processor_version|error_message)\b/)
    }
    // response_excerpt 在问询链里是「公司回复的摘录」，是业务内容；只在投递记录与审计页上
    // 它才是对方服务器的原始回包。
    for (const { path, text } of SOURCES.filter(({ path }) => /OutboxView|AuditView/.test(path))) {
      expect(text, path).not.toMatch(/\.response_excerpt\b/)
    }
  })

  it('不直接显示内部来源编码，显示来源名', () => {
    for (const { path, text } of SOURCES.filter(({ path }) => path.endsWith('.vue'))) {
      expect(text, path).not.toMatch(/\{\{[^}]*\.(source_id|root_source|representative_source|lead_source)\s*(\|\||\}\})/)
    }
  })

  it('不把整行原始记录打印到页面上', () => {
    for (const { path, text } of SOURCES.filter(({ path }) => path.endsWith('.vue'))) {
      expect(text, path).not.toMatch(/JSON\.stringify\(row\)/)
    }
  })
})
