import nextCoreWebVitals from 'eslint-config-next/core-web-vitals';
import nextTypescript from 'eslint-config-next/typescript';
import prettier from 'eslint-config-prettier/flat';

const eslintConfig = [
    ...nextCoreWebVitals,
    ...nextTypescript,
    prettier,
    {
        rules: {
            '@typescript-eslint/no-unused-vars': 'off',
            '@typescript-eslint/no-explicit-any': 'off',
            // React Compiler rules included by Next 16 are too strict for this app's
            // existing effect-driven data loading and animated ref option caching.
            // Keep the rest of the hooks/core-web-vitals rules enabled.
            'react-hooks/set-state-in-effect': 'off',
            'react-hooks/refs': 'off',
            // Generated/user-provided images can be data URLs or arbitrary remote URLs, so next/image is not always suitable here.
            '@next/next/no-img-element': 'off',
        },
    },
];

export default eslintConfig;
