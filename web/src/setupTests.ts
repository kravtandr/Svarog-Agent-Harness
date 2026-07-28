import "@testing-library/jest-dom/vitest";

// jsdom не реализует URL.createObjectURL/revokeObjectURL. ChatScreen рисует
// миниатюры вложений через blob-URL (fetch с Authorization, а не голый
// <img src>, который не может послать токен) — без этой заглушки любой
// тест, где в ленте есть вложение, падал бы на вызове несуществующего
// метода, а не на содержательной проверке.
if (typeof URL.createObjectURL !== "function") {
  URL.createObjectURL = () => "blob:mock";
}
if (typeof URL.revokeObjectURL !== "function") {
  URL.revokeObjectURL = () => {};
}
