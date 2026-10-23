const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const root = path.resolve(__dirname, '../..');
const source = fs.readFileSync(path.join(root, 'app/dev_site/assets/lesson-reader.js'), 'utf8');
const context = vm.createContext({});
vm.runInContext(source.slice(source.indexOf('function ocrSpeakerPrefix'), source.indexOf('function renderLessonCards')), context);

const footer = '(YBM 종 로 센터 서 재 연 )\nYBM 어학원 취 업 과 승진 필수 어 학 자격\n학 원 문의 02) 2267-0509 TOEIC. Speaking\n내 용 문의 02) 2000-0561';
function sample() {
  const rows = [
    ['Make some space.', 160, 10], ['자리를 마련하자.', 180, 50],
    ['함께 쓸 공간을', 20, 110], ['만드는 표현입니다.', 20, 135],
    ['A: Please move this box', 20, 210], ['so we can sit together.', 60, 235],
    ['B: Sure, I will put it', 20, 280], ['on the shelf.', 60, 305],
    ['A: 이 상자를 옮겨 줘.', 20, 360], ['함께 앉을 수 있게.', 60, 385],
    ['shelf 선반 / space 공간', 20, 470], ['together 함께', 20, 495],
    ...footer.split('\n').map((line, i) => [line, 20, 550 + i * 25]),
  ];
  const regions = rows.flatMap(([line, left, y]) => {
    let x = left;
    return line.split(' ').map(text => {
      const word = {text, bounding_box: {x, y, width: text.length * 8, height: 18}};
      x += text.length * 8 + 10;
      return word;
    });
  });
  return {raw_text: rows.map(row => row[0]).join('\n'), regions};
}

test('wrapped dialogue stays in complete turns; explanation reflows and vocabulary stays separate', () => {
  const ocr = sample();
  const blocks = context.ocrReadingBlocks(ocr.raw_text, ocr.regions);
  const turns = blocks.filter(block => block.type === 'dialogue');
  assert.equal(turns.length, 3);
  assert.equal(turns[0].lines.join(' '), 'A: Please move this box so we can sit together.');
  assert.equal(turns[1].lines.join(' '), 'B: Sure, I will put it on the shelf.');
  assert.equal(turns[2].lines.join(' '), 'A: 이 상자를 옮겨 줘. 함께 앉을 수 있게.');
  assert(blocks.some(block => block.lines.join(' ') === '함께 쓸 공간을 만드는 표현입니다.'));
  assert(!turns.some(block => /shelf 선반/.test(block.lines.join(' '))));
});

test('specified credit and advertising lines are excluded, preserving every other source line', () => {
  const ocr = sample();
  const blocks = context.ocrReadingBlocks(ocr.raw_text, ocr.regions);
  const expected = ocr.raw_text.split('\n').filter(line => !footer.split('\n').includes(line));
  assert.equal(JSON.stringify(blocks.flatMap(block => block.lines)), JSON.stringify(expected));
  assert.equal(context.lessonTextLines(ocr.raw_text).join('\n'), expected.join('\n'));
  assert(!context.ocrFooterLine('A: I attend a YBM class.'));
  assert(!context.ocrFooterLine('shelf: 선반'));
  for (const line of footer.split('\n')) assert(context.ocrFooterLine(line));
});

test('original six-section fixture retains all lesson lines, punctuation and Korean', () => {
  const fixture = JSON.parse(fs.readFileSync(path.join(root, 'tests/fixtures/lesson_ocr_original_six_sections.json'), 'utf8'));
  const blocks = context.ocrReadingBlocks(fixture.ocr.raw_text, fixture.ocr.regions);
  assert.equal(JSON.stringify(blocks.flatMap(block => block.lines)), JSON.stringify(fixture.ocr.raw_text.split('\n').filter(line => line.trim())));
  assert.equal(blocks.filter(block => block.type === 'heading').length, 6);
  assert.equal(context.ocrReadingBlocks('').length, 0);
});

test('colonless speakers require layout evidence and cannot consume another language', () => {
  const regions = [{text: 'A', bounding_box: {x: 20, y: 10, width: 12, height: 18}},
    {text: 'Hello', bounding_box: {x: 60, y: 10, width: 40, height: 18}}];
  assert(context.ocrSpeakerPrefix('A Hello there', regions, 20));
  assert(!context.ocrSpeakerPrefix('A normal sentence', [], 0));
  const blocks = context.ocrReadingBlocks('A: Hello\n안녕하세요\nB： Fine');
  assert.equal(blocks.length, 3);
  assert.equal(blocks[1].type, 'paragraph');
});

test('AI dialogue fields reflow wrapped lines into separate colonless speaker turns', () => {
  const turns = context.lessonDialogueTurns("A Don't rush.\nTake your time.\nB： All right!\nA 좋아,\n천천히 하자.");
  assert.equal(turns.length, 3);
  assert.equal(turns[0].text, "Don't rush. Take your time.");
  assert.equal(turns[1].speaker, 'B');
  assert.equal(turns[2].text, '좋아, 천천히 하자.');
});
