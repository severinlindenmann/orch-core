// Compatibility entry for the mock app and its existing tests. Production uses realApi.
export * from './apiFactory'
export { api, mockStore, resetMockStoreForTests } from './mockApi'
