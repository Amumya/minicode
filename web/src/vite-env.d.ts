/// <reference types="vite/client" />

// 这句是为了让 `import "./styles.css"` 这种副作用导入能通过类型检查。
// vite/client 里带了 *.css 的声明；不引用它，tsc 会报 TS2882。
