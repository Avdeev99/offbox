import './style.css';

const counter = document.querySelector('#counter');
const reset = document.querySelector('#reset');
let count = 0;

counter.addEventListener('click', () => {
  count += 1;
  counter.textContent = `Count: ${count}`;
});

reset.addEventListener('click', () => {
  count = 0;
  counter.textContent = 'Count: 0';
});
