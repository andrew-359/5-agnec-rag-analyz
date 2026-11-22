export const reviewChatHeader = 'X-Review-Chat';

export type ReviewUsage = { tokens: number | null; complete: boolean };

export type ReviewChatUsage = {
  answers: Array<ReviewUsage & { id: string; label: string; pending: boolean }>;
  total: ReviewUsage;
};
